"""A real language model behind the ClassifierModel protocol.

It answers with one category and one confidence and has no other way to speak. It imports the
classifier contract and the standard library, so there is no tool, gate, plan or ledger in
scope here even by accident; an import-linter contract pins that.

No SDK is used on purpose: one JSON POST is smaller to read, smaller to audit and adds no
dependency. Two providers are supported, one hosted and one local, because the runtime must
treat every model identically whatever produced the answer.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from rote.contracts.classifier import ClassificationRequest, ClassificationResponse
from rote.contracts.errors import ClassifierError

ANTHROPIC = "anthropic"
OLLAMA = "ollama"

ROTE_CLASSIFIER = "ROTE_CLASSIFIER"
DETERMINISTIC_MODE = "deterministic"
LLM_MODE = "llm"

PROVIDER_ENV = "ROTE_LLM_PROVIDER"
MODEL_ENV = "ROTE_LLM_MODEL"
ENDPOINT_ENV = "ROTE_LLM_ENDPOINT"
TIMEOUT_ENV = "ROTE_LLM_TIMEOUT_SECONDS"
ATTEMPTS_ENV = "ROTE_LLM_MAX_ATTEMPTS"

PROMPT_TEMPLATE_ID = "classify-structured-only-v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 2
MAX_ANSWER_TOKENS = 256
# retrying these cannot help: the request or the credential is wrong, not the connection
FATAL_HTTP_STATUS = frozenset({400, 401, 403, 404, 422})

Transport = Callable[[str, bytes, Mapping[str, str], float], bytes]

# it is told what it may answer and nothing about what happens next, because nothing it says
# is executed. No tool name appears here; a test asserts that.
SYSTEM_INSTRUCTION = (
    "You classify payment reconciliation exceptions. Reply with one JSON object and no other "
    'text, in exactly this shape: {"category": "<one of the allowed categories>", '
    '"confidence_per_mille": <integer 0-1000>}. '
    "confidence_per_mille is how sure you are, where 1000 is certain. If the evidence fits "
    "more than one category, pick the best one and lower your confidence to say so. "
    "Do not explain. Do not propose any action."
)

# Plain business descriptions of what each label MEANS, so the model is answering the real
# question rather than guessing our vocabulary from six words. Deliberately not the router's
# predicates: handing over the preconditions would make the task mechanical and would prove
# nothing about a model. fee_mismatch and partial_payment describe genuinely different events
# that look identical in the numbers, which is the difficulty this project exists to measure.
CATEGORY_BRIEF: dict[str, str] = {
    "timing_cutoff": (
        "the full amount arrived, but the bank posted it after our capture cut-off, so the "
        "same amount sits on two different dates"
    ),
    "fee_mismatch": (
        "the bank credited less than expected because the gateway deducted its processing fee"
    ),
    "fx_rounding": (
        "the bank credited a different currency, so the converted figure does not land exactly "
        "on our number"
    ),
    "transposed_reference": (
        "the amounts agree but the digits of the payment reference have been rearranged"
    ),
    "partial_payment": (
        "the customer genuinely paid less than the full amount, so the shortfall is an "
        "underpayment rather than a deducted fee"
    ),
    "duplicate_entry": "the same payment appears on more than one candidate bank line",
}

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    endpoint: str
    # empty when the provider needs no credential, as a locally hosted one does not
    api_key_env: str
    timeout_seconds: float
    max_attempts: int


_DEFAULTS: dict[str, ProviderConfig] = {
    ANTHROPIC: ProviderConfig(
        provider=ANTHROPIC,
        model="claude-sonnet-5",
        endpoint="https://api.anthropic.com/v1/messages",
        api_key_env="ANTHROPIC_API_KEY",
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    ),
    OLLAMA: ProviderConfig(
        provider=OLLAMA,
        model="qwen3:8b",
        endpoint="http://127.0.0.1:11434/api/chat",
        api_key_env="",
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    ),
}


def provider_config(
    *,
    provider: str,
    model: str | None = None,
    endpoint: str | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
) -> ProviderConfig:
    base = _DEFAULTS.get(provider)
    if base is None:
        known = ", ".join(sorted(_DEFAULTS))
        raise ClassifierError(f"unknown provider {provider!r}; supported providers are {known}")
    return ProviderConfig(
        provider=base.provider,
        model=model or base.model,
        endpoint=endpoint or base.endpoint,
        api_key_env=base.api_key_env,
        timeout_seconds=base.timeout_seconds if timeout_seconds is None else timeout_seconds,
        max_attempts=base.max_attempts if max_attempts is None else max_attempts,
    )


class LlmClassifier:
    """Turns structured evidence into one typed category. It can say nothing else."""

    prompt_template_id = PROMPT_TEMPLATE_ID

    def __init__(
        self,
        *,
        config: ProviderConfig,
        api_key: str | None = None,
        transport: Transport | None = None,
        may_read_untrusted: bool = False,
    ) -> None:
        if config.api_key_env and not (api_key or "").strip():
            raise ClassifierError(
                f"{config.provider} needs a credential and {config.api_key_env} is not set; "
                "refusing to start rather than falling back to a different classifier"
            )
        self._config = config
        self._api_key = (api_key or "").strip()
        self._transport = transport or _urllib_transport
        # D5 uses this to decide whether merchant free text may reach the model at all. A real
        # model is told nothing untrusted unless a research probe explicitly asks for it.
        self.is_local = may_read_untrusted
        self.model_id = f"{config.provider}:{config.model}"
        self.calls = 0
        self.prompt_characters = 0
        self.untrusted_withheld = 0
        self.tokens_in = 0
        self.tokens_out = 0

    # the credential must not reach a log line, a trace or a test failure message
    def __repr__(self) -> str:
        return f"LlmClassifier(model_id={self.model_id!r}, is_local={self.is_local})"

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def prompt_for(self, request: ClassificationRequest) -> str:
        described = "\n".join(
            f"- {category.value}: {CATEGORY_BRIEF.get(category.value, category.value)}"
            for category in request.allowed_categories
        )
        parts = [
            "Allowed categories:",
            described,
            "Structured evidence:",
            json.dumps(request.task_input, indent=2, sort_keys=True, default=str),
        ]
        if self.is_local and request.untrusted:
            # research probe only: this is the branch the adversarial experiment turns on
            parts.append("Free text attached to the case:")
            parts.extend(f"[{block.source_path}] {block.content}" for block in request.untrusted)
        return "\n".join(parts)

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        if not self.is_local:
            self.untrusted_withheld += len(request.untrusted)
        prompt = self.prompt_for(request)
        self.calls += 1
        self.prompt_characters += len(prompt)
        body = self._ask(prompt)
        self._count_tokens(body)
        return _parse_answer(_provider_text(self._config.provider, body))

    def _ask(self, prompt: str) -> dict[str, Any]:
        payload = json.dumps(_provider_payload(self._config, prompt)).encode("utf-8")
        headers = _provider_headers(self._config, self._api_key)
        raw = self._send(payload, headers)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ClassifierError(
                f"{self.model_id} answered with something that is not JSON: {error}"
            ) from error
        if not isinstance(body, dict):
            raise ClassifierError(
                f"{self.model_id} answered with a {type(body).__name__}, not an object"
            )
        return body

    # transport trouble is retried; anything the provider says is not, because a second
    # identical request cannot repair a malformed answer
    def _send(self, payload: bytes, headers: Mapping[str, str]) -> bytes:
        last: Exception | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return self._transport(
                    self._config.endpoint, payload, headers, self._config.timeout_seconds
                )
            except urllib.error.HTTPError as error:
                if error.code in FATAL_HTTP_STATUS:
                    raise ClassifierError(
                        f"{self.model_id} refused the request with HTTP {error.code}"
                    ) from error
                last = error
            except Exception as error:
                last = error
            if attempt == self._config.max_attempts:
                break
        raise ClassifierError(
            f"{self.model_id} could not be reached after {self._config.max_attempts} "
            f"attempt(s): {type(last).__name__}: {last}"
        ) from last

    def _count_tokens(self, body: Mapping[str, Any]) -> None:
        usage = body.get("usage")
        if isinstance(usage, Mapping):
            self.tokens_in += _as_count(usage.get("input_tokens"))
            self.tokens_out += _as_count(usage.get("output_tokens"))
            return
        self.tokens_in += _as_count(body.get("prompt_eval_count"))
        self.tokens_out += _as_count(body.get("eval_count"))


def classifier_from_env(env: Mapping[str, str]) -> LlmClassifier | None:
    """None means "use the deterministic default". A misconfiguration raises instead."""
    mode = env.get(ROTE_CLASSIFIER, DETERMINISTIC_MODE).strip().lower() or DETERMINISTIC_MODE
    if mode == DETERMINISTIC_MODE:
        return None
    if mode != LLM_MODE:
        raise ClassifierError(
            f"{ROTE_CLASSIFIER}={mode!r} is not a mode; use {DETERMINISTIC_MODE!r} or {LLM_MODE!r}"
        )
    config = provider_config(
        provider=env.get(PROVIDER_ENV, ANTHROPIC).strip().lower() or ANTHROPIC,
        model=env.get(MODEL_ENV) or None,
        endpoint=env.get(ENDPOINT_ENV) or None,
        timeout_seconds=_as_float(env.get(TIMEOUT_ENV)),
        max_attempts=_as_int(env.get(ATTEMPTS_ENV)),
    )
    return LlmClassifier(
        config=config, api_key=env.get(config.api_key_env) if config.api_key_env else None
    )


def _provider_payload(config: ProviderConfig, prompt: str) -> dict[str, Any]:
    if config.provider == ANTHROPIC:
        return {
            "model": config.model,
            "max_tokens": MAX_ANSWER_TOKENS,
            "temperature": 0,
            "system": SYSTEM_INSTRUCTION,
            "messages": [{"role": "user", "content": prompt}],
        }
    return {
        "model": config.model,
        "stream": False,
        # a reasoning model would otherwise spend most of its budget thinking about a
        # six-way choice; the answer we need is one enum value, not an argument for it
        "think": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
    }


def _provider_headers(config: ProviderConfig, api_key: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if config.provider == ANTHROPIC:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _provider_text(provider: str, body: Mapping[str, Any]) -> str:
    if provider == ANTHROPIC:
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ClassifierError("the provider body carries no content blocks")
        found = [
            block.get("text", "")
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        if not found:
            raise ClassifierError("the provider body carries no text block")
        return "".join(found)
    message = body.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return str(message["content"])
    if isinstance(body.get("response"), str):
        return str(body["response"])
    raise ClassifierError("the provider body carries no message content")


# the model's words are never trusted for shape: an answer that is not exactly one category
# and one in-range confidence is a failure, never a guess repaired into something usable
def _parse_answer(text: str) -> ClassificationResponse:
    stripped = _THINK_BLOCK.sub("", text).strip()
    found = _first_json_object(stripped)
    if found is None:
        raise ClassifierError(f"no JSON object in the answer: {stripped[:200]!r}")
    category = found.get("category")
    confidence = found.get("confidence_per_mille")
    if not isinstance(category, str):
        raise ClassifierError(f"the answer carries no category: {sorted(found)}")
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise ClassifierError(f"confidence_per_mille is {confidence!r}, not an integer")
    try:
        return ClassificationResponse(category=category, confidence_per_mille=confidence)
    except ValidationError as error:
        raise ClassifierError(f"the answer is not a classification: {error}") from error


def _first_json_object(text: str) -> dict[str, Any] | None:
    depth = 0
    start = -1
    for index, character in enumerate(text):
        if character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    found = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(found, dict):
                    return found
    return None


def _urllib_transport(url: str, data: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        read: bytes = response.read()
    return read


def _as_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError as error:
        raise ClassifierError(f"{TIMEOUT_ENV}={value!r} is not a number") from error


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError as error:
        raise ClassifierError(f"{ATTEMPTS_ENV}={value!r} is not a whole number") from error


__all__ = [
    "ANTHROPIC",
    "CATEGORY_BRIEF",
    "DETERMINISTIC_MODE",
    "LLM_MODE",
    "OLLAMA",
    "PROMPT_TEMPLATE_ID",
    "ROTE_CLASSIFIER",
    "SYSTEM_INSTRUCTION",
    "LlmClassifier",
    "ProviderConfig",
    "classifier_from_env",
    "provider_config",
]
