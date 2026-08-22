from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rote.contracts.canonical import canonical_bytes
from rote.contracts.common import Domain, UntrustedText
from rote.contracts.errors import BoundaryError, CanonicalisationError
from rote.contracts.ingestion import TaskInput
from rote.safety.redaction import redact

PATH_PREFIX = "$."


def ingest(
    raw: Mapping[str, Any],
    *,
    domain: Domain,
    untrusted_paths: Sequence[str],
    correlation_id: str,
) -> TaskInput:
    payload = dict(raw)
    blocks: list[UntrustedText] = []
    found: set[str] = set()

    for path in untrusted_paths:
        key = _top_level_key(path)
        if key not in payload:
            continue
        value = payload.pop(key)
        if not isinstance(value, str):
            raise BoundaryError(f"{path} was declared free text but holds {type(value).__name__}")
        cleaned, redactions = redact(value)
        found.update(redactions)
        blocks.append(UntrustedText.of(path, cleaned))

    structured, structural_redactions = _redact_structured(payload)
    found.update(structural_redactions)
    _require_canonicalisable(structured)

    return TaskInput(
        correlation_id=correlation_id,
        domain=domain,
        structured=structured,
        untrusted=tuple(blocks),
        redactions=tuple(sorted(found)),
    )


def _top_level_key(path: str) -> str:
    if not path.startswith(PATH_PREFIX):
        raise BoundaryError(f"{path!r} is not a json path")
    return path[len(PATH_PREFIX) :]


def _redact_structured(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    found: set[str] = set()

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            cleaned, redactions = redact(value)
            found.update(redactions)
            return cleaned
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(payload), found


# malformed input fails here, at the edge, rather than three layers in
def _require_canonicalisable(structured: dict[str, Any]) -> None:
    try:
        canonical_bytes(structured)
    except CanonicalisationError as error:
        raise BoundaryError(f"the structured payload cannot be canonicalised: {error}") from error
