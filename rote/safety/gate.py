from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from rote.contracts.canonical import canonical_hash
from rote.contracts.common import Currency
from rote.contracts.errors import PolicyError, RoteError
from rote.contracts.ledger import LedgerEvent, LedgerEventType
from rote.contracts.policy import PolicyConfig, PolicyContext, PolicyRule
from rote.contracts.tools import Toolbox, ToolSpec
from rote.contracts.trajectory import GateVerdict
from rote.observability.logging import get_logger
from rote.safety.ledger import Ledger

IDEMPOTENCY_ARG = "idempotency_key"

_logger = get_logger("rote.safety.gate")


@dataclass
class _Spend:
    at: datetime
    currency: Currency
    minor_units: int


@dataclass
class _ActionRecord:
    fingerprint: str
    settled: bool
    result: dict[str, Any] | None = field(default=None)


class PolicyGate:
    def __init__(
        self,
        *,
        adapters: Toolbox,
        config: PolicyConfig,
        ledger: Ledger,
        clock: Callable[[], datetime],
    ) -> None:
        self._adapters = adapters
        self._config = config
        self._ledger = ledger
        self._clock = clock
        self._spend: list[_Spend] = []
        self._actions: dict[str, _ActionRecord] = {}

    def for_task(self, context: PolicyContext, *, dry_run: bool = True) -> GatedToolbox:
        return GatedToolbox(gate=self, context=context, dry_run=dry_run)

    # a boundary is only as harmless as whatever sits behind it, and silence means it can act
    @property
    def mutates_the_world(self) -> bool:
        return bool(getattr(self._adapters, "mutates_the_world", True))

    def _offered_tools(self, context: PolicyContext) -> tuple[ToolSpec, ...]:
        rule = self._config.rule_for(context.path, context.category)
        if rule is None:
            return ()
        return tuple(
            _without_idempotency_argument(spec)
            for spec in self._adapters.available_tools()
            if spec.name in rule.allowed_tools
        )

    def _execute(
        self, context: PolicyContext, dry_run: bool, name: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        arguments = dict(payload)
        rule = self._config.rule_for(context.path, context.category)
        if rule is None:
            raise self._refuse(context, dry_run, name, "no policy rule covers this path")

        self._check_allowlist(context, dry_run, name, rule)
        mutating = name in self._config.require_idempotency_for
        if not mutating:
            self._record_verdict(context, dry_run, name, GateVerdict.PERMIT, "read permitted")
            return self._adapters.invoke(name, arguments)

        self._reject_caller_supplied_key(context, dry_run, name, arguments)
        self._check_amount(context, dry_run, name, rule, arguments)
        key = derive_idempotency_key(context.task_id, name, arguments)
        arguments[IDEMPOTENCY_ARG] = key
        fingerprint = canonical_hash({"tool": name, "arguments": arguments})
        replay = self._replay(context, dry_run, name, key, fingerprint)
        if replay is not None:
            return replay

        self._record_verdict(context, dry_run, name, GateVerdict.PERMIT, "within policy")
        self._actions[key] = _ActionRecord(fingerprint=fingerprint, settled=False)
        self._append(context, dry_run, LedgerEventType.INTENT, {"tool": name, "key": key})
        try:
            result = self._adapters.invoke(name, arguments)
        except RoteError as error:
            # we issued the instruction and do not know whether it landed, so a human decides
            self._append(
                context,
                dry_run,
                LedgerEventType.UNKNOWN,
                {"tool": name, "key": key, "error": type(error).__name__},
            )
            raise
        self._actions[key] = _ActionRecord(fingerprint=fingerprint, settled=True, result=result)
        self._record_spend(name, arguments)
        self._append(
            context,
            dry_run,
            LedgerEventType.OUTCOME,
            {"tool": name, "key": key, "result_hash": canonical_hash(result)},
        )
        return result

    def _check_allowlist(
        self, context: PolicyContext, dry_run: bool, name: str, rule: PolicyRule
    ) -> None:
        offered = {spec.name for spec in self._adapters.available_tools()}
        if name not in offered:
            raise self._refuse(context, dry_run, name, "no such tool at this boundary")
        if name not in rule.allowed_tools:
            raise self._refuse(context, dry_run, name, "tool is not allowlisted for this category")

    # the key is the gate's to compute: a caller that picks its own could force a replay
    def _reject_caller_supplied_key(
        self,
        context: PolicyContext,
        dry_run: bool,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        if IDEMPOTENCY_ARG in arguments:
            raise self._refuse(
                context, dry_run, name, "the gate derives the idempotency key, callers may not"
            )

    def _check_amount(
        self,
        context: PolicyContext,
        dry_run: bool,
        name: str,
        rule: PolicyRule,
        arguments: dict[str, Any],
    ) -> None:
        money = self._config.money_argument_for(name)
        if money is None:
            return
        currency = _as_currency(arguments.get(money.currency_arg))
        if currency is None or currency not in rule.max_per_action:
            raise self._refuse(context, dry_run, name, "no cap is declared for this currency")
        amount = abs(int(arguments[money.amount_arg]))
        if amount > rule.max_per_action[currency]:
            raise self._escalate(context, dry_run, name, "amount exceeds the per-action cap")
        if amount + self._window_spend(currency, rule) > rule.max_per_window.get(currency, 0):
            raise self._escalate(context, dry_run, name, "amount exceeds the rolling window cap")

    def _replay(
        self,
        context: PolicyContext,
        dry_run: bool,
        name: str,
        key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        prior = self._actions.get(key)
        if prior is None:
            return None
        if prior.fingerprint != fingerprint:
            raise self._refuse(
                context, dry_run, name, "this idempotency key was used for a different action"
            )
        if not prior.settled:
            raise self._escalate(
                context, dry_run, name, "the earlier attempt is UNKNOWN and needs a human"
            )
        self._record_verdict(context, dry_run, name, GateVerdict.PERMIT, "idempotent replay")
        return prior.result

    def _window_spend(self, currency: Currency, rule: PolicyRule) -> int:
        cutoff = self._clock() - timedelta(seconds=rule.window_seconds)
        self._spend = [entry for entry in self._spend if entry.at > cutoff]
        return sum(entry.minor_units for entry in self._spend if entry.currency is currency)

    def _record_spend(self, name: str, arguments: dict[str, Any]) -> None:
        money = self._config.money_argument_for(name)
        if money is None:
            return
        currency = _as_currency(arguments.get(money.currency_arg))
        if currency is None:
            return
        self._spend.append(
            _Spend(
                at=self._clock(),
                currency=currency,
                minor_units=abs(int(arguments[money.amount_arg])),
            )
        )

    def _refuse(self, context: PolicyContext, dry_run: bool, name: str, reason: str) -> PolicyError:
        self._record_verdict(context, dry_run, name, GateVerdict.REFUSE, reason)
        return PolicyError(GateVerdict.REFUSE, reason)

    def _escalate(
        self, context: PolicyContext, dry_run: bool, name: str, reason: str
    ) -> PolicyError:
        self._record_verdict(context, dry_run, name, GateVerdict.ESCALATE, reason)
        return PolicyError(GateVerdict.ESCALATE, reason)

    def _record_verdict(
        self,
        context: PolicyContext,
        dry_run: bool,
        name: str,
        verdict: GateVerdict,
        reason: str,
    ) -> None:
        self._append(
            context,
            dry_run,
            LedgerEventType.GATE_VERDICT,
            {"tool": name, "verdict": verdict.value, "reason": reason},
        )
        _logger.info(
            "gate_decision",
            correlation_id=context.correlation_id,
            task_id=context.task_id,
            path=context.path.value,
            category=None if context.category is None else context.category.value,
            tool=name,
            verdict=verdict.value,
            reason=reason,
            dry_run=dry_run,
        )

    def _append(
        self,
        context: PolicyContext,
        dry_run: bool,
        event_type: LedgerEventType,
        payload: dict[str, Any],
    ) -> None:
        self._ledger.append(
            LedgerEvent(
                correlation_id=context.correlation_id,
                task_id=context.task_id,
                event_type=event_type,
                actor=context.actor,
                payload=payload,
                dry_run=dry_run,
                occurred_at=self._clock(),
            )
        )


class GatedToolbox:
    enforces_policy = True

    def __init__(self, *, gate: PolicyGate, context: PolicyContext, dry_run: bool) -> None:
        self._gate = gate
        self._context = context
        self._dry_run = dry_run

    @property
    def mutates_the_world(self) -> bool:
        return self._gate.mutates_the_world

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return self._gate._offered_tools(self._context)

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._gate._execute(self._context, self._dry_run, name, payload)


def derive_idempotency_key(task_id: str, tool: str, arguments: dict[str, Any]) -> str:
    without_key = {k: v for k, v in arguments.items() if k != IDEMPOTENCY_ARG}
    digest = canonical_hash({"task_id": task_id, "tool": tool, "arguments": without_key})
    return f"{task_id}:{tool}:{digest[:16]}"


# the key is not the agent's business, so it is not in the interface the agent is shown
def _without_idempotency_argument(spec: ToolSpec) -> ToolSpec:
    parameters = dict(spec.parameters)
    properties = parameters.get("properties")
    if isinstance(properties, dict) and IDEMPOTENCY_ARG in properties:
        parameters["properties"] = {k: v for k, v in properties.items() if k != IDEMPOTENCY_ARG}
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [name for name in required if name != IDEMPOTENCY_ARG]
    return spec.model_copy(update={"parameters": parameters})


def _as_currency(value: object) -> Currency | None:
    if isinstance(value, Currency):
        return value
    if isinstance(value, str):
        try:
            return Currency(value)
        except ValueError:
            return None
    return None
