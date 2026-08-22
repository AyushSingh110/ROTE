from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rote.contracts.common import Currency, ExceptionCategory
from rote.contracts.errors import PolicyError, RecordNotFoundError, ToolError
from rote.contracts.ledger import LedgerEventType
from rote.contracts.policy import (
    ExecutionPath,
    MoneyArgument,
    PolicyConfig,
    PolicyContext,
    PolicyRule,
)
from rote.contracts.reconciliation import GeneratedDataset, WorldSnapshot
from rote.contracts.tools import Toolbox, ToolSpec
from rote.contracts.trajectory import GateVerdict
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import OBSERVATIONAL_TOOLS, default_policy_config

SEED = 29
COUNT = 30
READ_TOOLS = frozenset(
    {
        "get_settlement_record",
        "get_bank_line",
        "find_bank_lines_by_amount",
        "list_bank_lines_for_reference",
        "get_fee_schedule",
        "get_fx_rate",
    }
)
WRITE_TOOLS = frozenset({"post_adjustment", "mark_settlement_matched", "void_duplicate_bank_line"})


def ticks() -> Iterator[datetime]:
    moment = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(seconds=1)


class SettableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def dataset(count: int = COUNT) -> GeneratedDataset:
    return generate_dataset(seed=SEED, count=count)


class CountingTools:
    enforces_policy = False

    def __init__(self, inner: ReconciliationTools) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return self._inner.available_tools()

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(name)
        return self._inner.invoke(name, payload)

    def snapshot(self) -> WorldSnapshot:
        return self._inner.snapshot()


class ExplodingTools:
    enforces_policy = False

    def __init__(self, inner: ReconciliationTools, explode_on: str) -> None:
        self._inner = inner
        self._explode_on = explode_on
        self.calls: list[str] = []

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return self._inner.available_tools()

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(name)
        if name == self._explode_on:
            raise RecordNotFoundError("simulated failure after the instruction was issued")
        return self._inner.invoke(name, payload)


def build(
    *,
    config: PolicyConfig | None = None,
    tools: Toolbox | None = None,
    count: int = COUNT,
) -> tuple[GeneratedDataset, PolicyGate, Ledger, Any]:
    data = dataset(count)
    adapters = tools if tools is not None else ReconciliationTools.from_snapshot(data.world)
    ledger = Ledger()
    clock = ticks()
    gate = PolicyGate(
        adapters=adapters,
        config=config or default_policy_config(),
        ledger=ledger,
        clock=lambda: next(clock),
    )
    return data, gate, ledger, adapters


def context(
    data: GeneratedDataset,
    index: int = 0,
    path: ExecutionPath = ExecutionPath.LIVE_AGENT,
    category: ExceptionCategory | None = None,
) -> PolicyContext:
    exception = data.exceptions[index]
    return PolicyContext(
        task_id=exception.exception_id,
        correlation_id=f"{exception.exception_id}:run-0",
        path=path,
        category=category,
        actor="system:agent",
    )


def tight_config(per_action: int) -> PolicyConfig:
    return PolicyConfig(
        rules=(
            PolicyRule(
                path=ExecutionPath.LIVE_AGENT,
                category=None,
                allowed_tools=frozenset(READ_TOOLS | WRITE_TOOLS),
                max_per_action={Currency.INR: per_action},
                max_per_window={Currency.INR: per_action * 3},
                window_seconds=3600,
            ),
        ),
        money_arguments=(
            MoneyArgument(
                tool="post_adjustment", amount_arg="minor_units", currency_arg="currency"
            ),
        ),
        require_idempotency_for=frozenset(WRITE_TOOLS),
    )


def adjust(data: GeneratedDataset, amount: int) -> dict[str, Any]:
    return {
        "record_id": data.exceptions[0].facts.record_id,
        "minor_units": amount,
        "currency": "INR",
        "reason": "fee",
    }


def adjust_with_key(data: GeneratedDataset, amount: int) -> dict[str, Any]:
    return {**adjust(data, amount), "idempotency_key": "caller-chosen"}


class TestTheGateIsTheToolBoundary:
    def test_the_gated_toolbox_satisfies_the_toolbox_protocol(self) -> None:
        data, gate, _ledger, _tools = build()
        box: Toolbox = gate.for_task(context(data))
        assert box.available_tools()

    def test_the_gated_toolbox_declares_that_it_enforces_policy(self) -> None:
        data, gate, _ledger, _tools = build()
        assert gate.for_task(context(data)).enforces_policy is True

    def test_a_raw_adapter_declares_that_it_enforces_nothing(self) -> None:
        data = dataset(6)
        assert ReconciliationTools.from_snapshot(data.world).enforces_policy is False

    def test_the_agent_is_only_shown_tools_it_is_allowed_to_use(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        offered = {spec.name for spec in gate.for_task(context(data)).available_tools()}
        assert offered == READ_TOOLS | WRITE_TOOLS
        assert "get_chargeback_history" not in offered

    def test_a_tool_outside_the_allowlist_is_refused_even_when_called_directly(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        box = gate.for_task(context(data))
        with pytest.raises(PolicyError) as raised:
            box.invoke("get_chargeback_history", {"order_id": "ORD-000000"})
        assert raised.value.verdict is GateVerdict.REFUSE

    def test_a_refused_call_never_reaches_the_adapter(self) -> None:
        data = dataset(6)
        counting = CountingTools(ReconciliationTools.from_snapshot(data.world))
        _data, gate, _ledger, _t = build(config=tight_config(50_000), tools=counting, count=6)
        with pytest.raises(PolicyError):
            gate.for_task(context(data)).invoke("get_chargeback_history", {"order_id": "x"})
        assert counting.calls == []

    def test_a_permitted_read_passes_through_unchanged(self) -> None:
        data, gate, _ledger, _tools = build()
        box = gate.for_task(context(data))
        result = box.invoke(
            "get_settlement_record", {"record_id": data.exceptions[0].facts.record_id}
        )
        assert result["record"]["record_id"] == data.exceptions[0].facts.record_id

    def test_an_unknown_tool_is_refused_rather_than_forwarded(self) -> None:
        data, gate, _ledger, _tools = build()
        with pytest.raises(PolicyError):
            gate.for_task(context(data)).invoke("wire_money_anywhere", {})


class TestMonetaryCaps:
    def test_an_amount_within_the_cap_is_permitted(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        assert gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 1_000))

    def test_an_amount_over_the_per_action_cap_escalates(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        with pytest.raises(PolicyError) as raised:
            gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 50_001))
        assert raised.value.verdict is GateVerdict.ESCALATE

    def test_the_cap_looks_at_magnitude_not_sign(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        with pytest.raises(PolicyError):
            gate.for_task(context(data)).invoke("post_adjustment", adjust(data, -50_001))

    def test_an_over_cap_action_never_reaches_the_adapter(self) -> None:
        data = dataset(6)
        counting = CountingTools(ReconciliationTools.from_snapshot(data.world))
        _d, gate, _ledger, _t = build(config=tight_config(1_000), tools=counting, count=6)
        with pytest.raises(PolicyError):
            gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 9_999))
        assert counting.calls == []

    def test_the_rolling_window_accumulates_across_calls(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(1_000))
        box = gate.for_task(context(data))
        for amount in (1_000, 999, 998):
            box.invoke("post_adjustment", adjust(data, amount))
        with pytest.raises(PolicyError) as raised:
            box.invoke("post_adjustment", adjust(data, 997))
        assert raised.value.verdict is GateVerdict.ESCALATE

    def test_spend_outside_the_window_no_longer_counts(self) -> None:
        data = dataset(6)
        clock = SettableClock(datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC))
        gate = PolicyGate(
            adapters=ReconciliationTools.from_snapshot(data.world),
            config=tight_config(1_000),
            ledger=Ledger(),
            clock=clock,
        )
        box = gate.for_task(context(data))
        for amount in (1_000, 999, 998):
            box.invoke("post_adjustment", adjust(data, amount))
        with pytest.raises(PolicyError):
            box.invoke("post_adjustment", adjust(data, 997))

        clock.now += timedelta(hours=2)
        assert box.invoke("post_adjustment", adjust(data, 996))

    def test_a_currency_with_no_declared_cap_is_refused(self) -> None:
        data, gate, _ledger, _tools = build(config=tight_config(50_000))
        payload = {**adjust(data, 10), "currency": "EUR"}
        with pytest.raises(PolicyError) as raised:
            gate.for_task(context(data)).invoke("post_adjustment", payload)
        assert raised.value.verdict is GateVerdict.REFUSE


class TestAmendmentA2NoImplicitLiveAgentPrivilege:
    def test_both_paths_get_the_same_default_cap(self) -> None:
        config = default_policy_config()
        live = config.rule_for(ExecutionPath.LIVE_AGENT, None)
        compiled = config.rule_for(ExecutionPath.COMPILED_PLAN, None)
        assert live is not None
        assert compiled is not None
        assert live.max_per_action == compiled.max_per_action

    def test_neither_path_may_exceed_its_cap(self) -> None:
        config = default_policy_config()
        over = config.rule_for(ExecutionPath.LIVE_AGENT, None)
        assert over is not None
        amount = max(over.max_per_action.values()) + 1
        for path in (ExecutionPath.LIVE_AGENT, ExecutionPath.COMPILED_PLAN):
            data, gate, _ledger, _tools = build()
            with pytest.raises(PolicyError) as raised:
                gate.for_task(context(data, path=path)).invoke(
                    "post_adjustment", adjust(data, amount)
                )
            assert raised.value.verdict is GateVerdict.ESCALATE

    def test_an_unknown_category_falls_back_to_the_strictest_rule(self) -> None:
        config = default_policy_config()
        fallback = config.rule_for(ExecutionPath.LIVE_AGENT, None)
        assert fallback is not None
        for category in ExceptionCategory:
            specific = config.rule_for(ExecutionPath.LIVE_AGENT, category)
            assert specific is not None
            for currency, cap in specific.max_per_action.items():
                assert cap <= fallback.max_per_action.get(currency, cap)

    def test_a_category_may_narrow_the_allowlist(self) -> None:
        config = default_policy_config()
        fee = config.rule_for(ExecutionPath.LIVE_AGENT, ExceptionCategory.FEE_MISMATCH)
        assert fee is not None
        assert "void_duplicate_bank_line" not in fee.allowed_tools

    def test_a_fee_plan_may_not_void_a_bank_line(self) -> None:
        data, gate, _ledger, _tools = build()
        box = gate.for_task(context(data, category=ExceptionCategory.FEE_MISMATCH))
        with pytest.raises(PolicyError):
            box.invoke(
                "void_duplicate_bank_line",
                {"line_id": data.world.bank_lines[0].line_id},
            )


class TestIdempotencyAndAtMostOnce:
    def test_intent_is_written_before_outcome(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))
        kinds = [entry.event_type for entry in ledger.entries]
        assert kinds.index(LedgerEventType.INTENT) < kinds.index(LedgerEventType.OUTCOME)

    def test_a_read_writes_no_intent(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke(
            "get_settlement_record", {"record_id": data.exceptions[0].facts.record_id}
        )
        assert LedgerEventType.INTENT not in {e.event_type for e in ledger.entries}

    def test_replaying_the_same_action_returns_the_same_result(self) -> None:
        data, gate, _ledger, _tools = build()
        box = gate.for_task(context(data))
        first = box.invoke("post_adjustment", adjust(data, 100))
        second = box.invoke("post_adjustment", adjust(data, 100))
        assert first == second

    def test_replaying_the_same_action_never_calls_the_adapter_again(self) -> None:
        data = dataset(6)
        counting = CountingTools(ReconciliationTools.from_snapshot(data.world))
        _d, gate, _ledger, _t = build(tools=counting, count=6)
        box = gate.for_task(context(data))
        box.invoke("post_adjustment", adjust(data, 100))
        box.invoke("post_adjustment", adjust(data, 100))
        assert counting.calls.count("post_adjustment") == 1

    def test_a_different_amount_is_a_different_action_not_a_replay(self) -> None:
        data, gate, _ledger, _tools = build()
        box = gate.for_task(context(data))
        first = box.invoke("post_adjustment", adjust(data, 100))
        second = box.invoke("post_adjustment", adjust(data, 200))
        assert first != second


class TestUnknownOutcome:
    def test_a_failure_after_intent_is_recorded_as_unknown(self) -> None:
        data = dataset(6)
        exploding = ExplodingTools(ReconciliationTools.from_snapshot(data.world), "post_adjustment")
        _d, gate, ledger, _t = build(tools=exploding, count=6)
        with pytest.raises(ToolError):
            gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))
        kinds = [entry.event_type for entry in ledger.entries]
        assert LedgerEventType.INTENT in kinds
        assert LedgerEventType.UNKNOWN in kinds
        assert LedgerEventType.OUTCOME not in kinds

    def test_an_unknown_action_is_never_automatically_retried(self) -> None:
        data = dataset(6)
        exploding = ExplodingTools(ReconciliationTools.from_snapshot(data.world), "post_adjustment")
        _d, gate, _ledger, _t = build(tools=exploding, count=6)
        box = gate.for_task(context(data))
        with pytest.raises(ToolError):
            box.invoke("post_adjustment", adjust(data, 100))
        before = exploding.calls.count("post_adjustment")
        with pytest.raises(PolicyError) as raised:
            box.invoke("post_adjustment", adjust(data, 100))
        assert raised.value.verdict is GateVerdict.ESCALATE
        assert exploding.calls.count("post_adjustment") == before

    def test_an_unknown_action_does_not_double_post(self) -> None:
        data = dataset(6)
        inner = ReconciliationTools.from_snapshot(data.world)
        exploding = ExplodingTools(inner, "post_adjustment")
        _d, gate, _ledger, _t = build(tools=exploding, count=6)
        box = gate.for_task(context(data))
        for _ in range(3):
            with pytest.raises((ToolError, PolicyError)):
                box.invoke("post_adjustment", adjust(data, 100))
        assert inner.snapshot().adjustments == ()

    def test_a_refusal_before_intent_is_not_unknown(self) -> None:
        data, gate, ledger, _tools = build(config=tight_config(10))
        with pytest.raises(PolicyError):
            gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 5_000))
        kinds = {entry.event_type for entry in ledger.entries}
        assert LedgerEventType.UNKNOWN not in kinds
        assert LedgerEventType.INTENT not in kinds


class TestLedgerRecording:
    def test_every_decision_is_recorded_permit_and_refuse_alike(self) -> None:
        data, gate, ledger, _tools = build(config=tight_config(50_000))
        box = gate.for_task(context(data))
        box.invoke("get_settlement_record", {"record_id": data.exceptions[0].facts.record_id})
        with pytest.raises(PolicyError):
            box.invoke("get_chargeback_history", {"order_id": "ORD-000000"})
        verdicts = [
            entry for entry in ledger.entries if entry.event_type is LedgerEventType.GATE_VERDICT
        ]
        assert len(verdicts) == 2

    def test_the_chain_stays_valid_after_many_decisions(self) -> None:
        data, gate, ledger, _tools = build()
        box = gate.for_task(context(data))
        for index in range(10):
            box.invoke("post_adjustment", adjust(data, 100 + index))
        assert ledger.verify().valid is True

    def test_the_dry_run_flag_reaches_the_ledger(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data), dry_run=True).invoke(
            "get_settlement_record", {"record_id": data.exceptions[0].facts.record_id}
        )
        assert all(entry.dry_run for entry in ledger.entries)

    def test_dry_run_is_the_default(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke(
            "get_settlement_record", {"record_id": data.exceptions[0].facts.record_id}
        )
        assert all(entry.dry_run for entry in ledger.entries)

    def test_the_correlation_id_is_carried_into_every_entry(self) -> None:
        data, gate, ledger, _tools = build()
        ctx = context(data)
        gate.for_task(ctx).invoke("post_adjustment", adjust(data, 100))
        assert all(entry.correlation_id == ctx.correlation_id for entry in ledger.entries)

    def test_no_secret_shaped_value_reaches_the_ledger(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))
        for entry in ledger.entries:
            rendered = str(entry.payload).lower()
            for marker in ("api_key", "password", "secret", "authorization", "bearer "):
                assert marker not in rendered


class TestObservationalToolsGrantNoAuthority:
    def test_read_only_decoys_are_offered_to_the_agent(self) -> None:
        data, gate, _ledger, _tools = build()
        offered = {spec.name for spec in gate.for_task(context(data)).available_tools()}
        assert offered >= OBSERVATIONAL_TOOLS

    def test_no_observational_tool_is_mutating(self) -> None:
        assert OBSERVATIONAL_TOOLS.isdisjoint(WRITE_TOOLS)

    def test_an_observational_call_writes_no_intent(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke(
            "get_merchant_profile", {"merchant_id": data.exceptions[0].facts.merchant_id}
        )
        assert LedgerEventType.INTENT not in {e.event_type for e in ledger.entries}

    def test_an_observational_call_is_still_recorded_as_a_gate_decision(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke(
            "get_merchant_profile", {"merchant_id": data.exceptions[0].facts.merchant_id}
        )
        verdicts = [e for e in ledger.entries if e.event_type is LedgerEventType.GATE_VERDICT]
        assert len(verdicts) == 1
        assert verdicts[0].payload["verdict"] == "permit"

    def test_money_caps_are_untouched_by_the_change(self) -> None:
        config = default_policy_config()
        rule = config.rule_for(ExecutionPath.LIVE_AGENT, None)
        assert rule is not None
        assert rule.max_per_action == {Currency.INR: 50_000, Currency.USD: 1_000}

    def test_a_fee_plan_still_cannot_void_a_bank_line(self) -> None:
        config = default_policy_config()
        fee = config.rule_for(ExecutionPath.LIVE_AGENT, ExceptionCategory.FEE_MISMATCH)
        assert fee is not None
        assert "void_duplicate_bank_line" not in fee.allowed_tools

    def test_observational_tools_are_available_in_every_category(self) -> None:
        config = default_policy_config()
        for path in ExecutionPath:
            for category in (None, *ExceptionCategory):
                rule = config.rule_for(path, category)
                assert rule is not None
                assert rule.allowed_tools >= OBSERVATIONAL_TOOLS

    def test_both_paths_still_offer_the_same_tools(self) -> None:
        config = default_policy_config()
        live = config.rule_for(ExecutionPath.LIVE_AGENT, None)
        compiled = config.rule_for(ExecutionPath.COMPILED_PLAN, None)
        assert live is not None
        assert compiled is not None
        assert live.allowed_tools == compiled.allowed_tools


class TestTheGateOwnsTheIdempotencyKey:
    def test_a_caller_supplied_key_is_refused(self) -> None:
        data, gate, _ledger, _tools = build()
        with pytest.raises(PolicyError) as raised:
            gate.for_task(context(data)).invoke("post_adjustment", adjust_with_key(data, 100))
        assert raised.value.verdict is GateVerdict.REFUSE
        assert "derive" in raised.value.reason

    def test_a_mutating_call_needs_no_key_from_the_caller(self) -> None:
        data, gate, _ledger, _tools = build()
        assert gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))

    def test_the_key_never_appears_in_the_advertised_interface(self) -> None:
        data, gate, _ledger, _tools = build()
        for spec in gate.for_task(context(data)).available_tools():
            assert "idempotency_key" not in spec.parameters.get("properties", {})
            assert "idempotency_key" not in spec.parameters.get("required", [])

    def test_the_same_action_derives_the_same_key(self) -> None:
        data, gate, ledger, _tools = build()
        box = gate.for_task(context(data))
        box.invoke("post_adjustment", adjust(data, 100))
        box.invoke("post_adjustment", adjust(data, 100))
        intents = [e for e in ledger.entries if e.event_type is LedgerEventType.INTENT]
        assert len(intents) == 1

    def test_a_different_amount_derives_a_different_key(self) -> None:
        data, gate, ledger, _tools = build()
        box = gate.for_task(context(data))
        box.invoke("post_adjustment", adjust(data, 100))
        box.invoke("post_adjustment", adjust(data, 200))
        intents = [e for e in ledger.entries if e.event_type is LedgerEventType.INTENT]
        assert len(intents) == 2

    def test_the_same_action_for_a_different_task_derives_a_different_key(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data, 0)).invoke("post_adjustment", adjust(data, 100))
        gate.for_task(context(data, 1)).invoke("post_adjustment", adjust(data, 100))
        intents = [e for e in ledger.entries if e.event_type is LedgerEventType.INTENT]
        assert len(intents) == 2

    def test_the_derived_key_reaches_the_adapter(self) -> None:
        data = dataset(6)
        inner = ReconciliationTools.from_snapshot(data.world)
        _d, gate, _ledger, _t = build(tools=inner, count=6)
        gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))
        posted = inner.snapshot().adjustments
        assert len(posted) == 1
        assert posted[0].idempotency_key.startswith(data.exceptions[0].exception_id)

    def test_the_ledger_records_the_derived_key(self) -> None:
        data, gate, ledger, _tools = build()
        gate.for_task(context(data)).invoke("post_adjustment", adjust(data, 100))
        intent = next(e for e in ledger.entries if e.event_type is LedgerEventType.INTENT)
        assert intent.payload["key"]
