import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from rote.agent.loop import run_agent
from rote.agent.models.offline import OfflineHeuristicModel
from rote.contracts.agent import AgentBudget
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Currency
from rote.contracts.ledger import LedgerEventType
from rote.contracts.policy import ExecutionPath, PolicyContext
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.trajectory import GateVerdict, Trajectory
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.observability.logging import configure_logging, get_logger
from rote.recorder.labelling import label_trajectory
from rote.recorder.recorder import TrajectoryRecorder
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import default_policy_config

SEED = 31
COUNT = 40


def ticks() -> Iterator[datetime]:
    moment = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(seconds=1)


def campaign(count: int = COUNT) -> tuple[GeneratedDataset, Ledger, list[Trajectory]]:
    data = generate_dataset(seed=SEED, count=count)
    adapters = ReconciliationTools.from_snapshot(data.world)
    ledger = Ledger()
    clock = ticks()
    gate = PolicyGate(
        adapters=adapters,
        config=default_policy_config(),
        ledger=ledger,
        clock=lambda: next(clock),
    )
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    trajectories: list[Trajectory] = []
    for exception in data.exceptions:
        trajectories.append(_run_one(exception, gate, adapters, truths[exception.exception_id]))
    return data, ledger, trajectories


def _run_one(
    exception: ReconciliationException,
    gate: PolicyGate,
    adapters: ReconciliationTools,
    truth: object,
) -> Trajectory:
    recorder_clock = ticks()
    toolbox = gate.for_task(
        PolicyContext(
            task_id=exception.exception_id,
            correlation_id=f"{exception.exception_id}:run-0",
            path=ExecutionPath.LIVE_AGENT,
            category=None,
            actor="system:agent",
        )
    )
    trajectory = run_agent(
        domain=exception.domain,
        task_input=exception.facts.model_dump(mode="json"),
        untrusted=exception.untrusted,
        toolbox=toolbox,
        model=OfflineHeuristicModel(seed=1),
        recorder=TrajectoryRecorder(clock=lambda: next(recorder_clock)),
        budget=AgentBudget(max_steps=12, max_tool_errors=3),
        correlation_id=f"{exception.exception_id}:run-0",
    )
    return label_trajectory(
        trajectory,
        facts=exception.facts,
        ground_truth=truth,  # type: ignore[arg-type]
        world=adapters.snapshot(),
    )


class TestEveryToolCallGoesThroughTheGate:
    def test_no_step_is_recorded_as_ungated_any_more(self) -> None:
        _data, _ledger, trajectories = campaign()
        verdicts = {step.gate_verdict for t in trajectories for step in t.steps}
        assert GateVerdict.UNGATED not in verdicts

    def test_every_recorded_step_carries_a_permit(self) -> None:
        _data, _ledger, trajectories = campaign()
        verdicts = {step.gate_verdict for t in trajectories for step in t.steps}
        assert verdicts == {GateVerdict.PERMIT}

    def test_the_ledger_holds_one_verdict_per_recorded_step(self) -> None:
        _data, ledger, trajectories = campaign()
        recorded_steps = sum(len(t.steps) for t in trajectories)
        gate_verdicts = sum(
            1 for entry in ledger.entries if entry.event_type is LedgerEventType.GATE_VERDICT
        )
        assert gate_verdicts == recorded_steps

    def test_the_audit_chain_survives_a_whole_campaign(self) -> None:
        _data, ledger, _trajectories = campaign()
        assert ledger.verify().valid is True

    def test_resolution_quality_is_unchanged_by_the_gate(self) -> None:
        _data, _ledger, trajectories = campaign()
        assert {t.checker_verdict for t in trajectories} == {CheckerVerdict.PASS}

    def test_every_mutating_call_has_a_matching_intent_and_outcome(self) -> None:
        _data, ledger, _trajectories = campaign()
        kinds = [entry.event_type for entry in ledger.entries]
        assert kinds.count(LedgerEventType.INTENT) == kinds.count(LedgerEventType.OUTCOME)
        assert kinds.count(LedgerEventType.INTENT) > 0

    def test_nothing_was_left_in_an_unknown_state(self) -> None:
        _data, ledger, _trajectories = campaign()
        assert LedgerEventType.UNKNOWN not in {e.event_type for e in ledger.entries}


class TestStructuredLogging:
    def test_events_are_emitted_as_json(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        get_logger("test").info("gate_decision", correlation_id="EXC-1:run-0", verdict="permit")
        payload = json.loads(stream.getvalue().strip())
        assert payload["event"] == "gate_decision"
        assert payload["correlation_id"] == "EXC-1:run-0"

    def test_every_event_carries_a_timestamp_and_level(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        get_logger("test").info("something", correlation_id="c-1")
        payload = json.loads(stream.getvalue().strip())
        assert "timestamp" in payload
        assert payload["level"] == "info"

    def test_secret_shaped_fields_are_scrubbed(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        get_logger("test").info(
            "call",
            correlation_id="c-1",
            api_key="sk-live-abcdef",
            password="hunter2",
            authorization="Bearer xyz",
            record_id="REC-000001",
        )
        rendered = stream.getvalue()
        assert "sk-live-abcdef" not in rendered
        assert "hunter2" not in rendered
        assert "Bearer xyz" not in rendered
        assert "REC-000001" in rendered

    def test_the_gate_logs_every_decision_with_its_correlation_id(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        data = generate_dataset(seed=SEED, count=6)
        clock = ticks()
        gate = PolicyGate(
            adapters=ReconciliationTools.from_snapshot(data.world),
            config=default_policy_config(),
            ledger=Ledger(),
            clock=lambda: next(clock),
        )
        gate.for_task(
            PolicyContext(
                task_id="EXC-000000",
                correlation_id="EXC-000000:run-0",
                path=ExecutionPath.LIVE_AGENT,
                category=None,
                actor="system:agent",
            )
        ).invoke("get_settlement_record", {"record_id": data.exceptions[0].facts.record_id})
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        decisions = [e for e in events if e["event"] == "gate_decision"]
        assert decisions
        assert all(e["correlation_id"] == "EXC-000000:run-0" for e in decisions)


class TestEscalationStopsTheRun:
    def _tight_campaign(self, per_action: int) -> tuple[Ledger, list[Trajectory], object]:
        data = generate_dataset(seed=SEED, count=COUNT)
        adapters = ReconciliationTools.from_snapshot(data.world)
        ledger = Ledger()
        clock = ticks()
        base = default_policy_config()
        config = base.model_copy(
            update={
                "rules": tuple(
                    rule.model_copy(
                        update={"max_per_action": {Currency.INR: per_action, Currency.USD: 10}}
                    )
                    for rule in base.rules
                )
            }
        )
        gate = PolicyGate(
            adapters=adapters, config=config, ledger=ledger, clock=lambda: next(clock)
        )
        truths = {truth.exception_id: truth for truth in data.ground_truths}
        trajectories = [
            _run_one(exception, gate, adapters, truths[exception.exception_id])
            for exception in data.exceptions
        ]
        return ledger, trajectories, adapters

    def test_a_gate_escalation_ends_the_run(self) -> None:
        _ledger, trajectories, _adapters = self._tight_campaign(3_000)
        escalated = [t for t in trajectories if t.outcome == "escalated"]
        assert escalated

    def test_an_escalated_step_is_recorded_with_the_escalate_verdict(self) -> None:
        _ledger, trajectories, _adapters = self._tight_campaign(3_000)
        verdicts = {step.gate_verdict for t in trajectories for step in t.steps}
        assert GateVerdict.ESCALATE in verdicts

    def test_the_agent_never_acts_after_an_escalation(self) -> None:
        _ledger, trajectories, _adapters = self._tight_campaign(3_000)
        for trajectory in trajectories:
            for index, step in enumerate(trajectory.steps):
                if step.gate_verdict is GateVerdict.ESCALATE:
                    assert index == len(trajectory.steps) - 1

    def test_escalation_turns_a_wrong_answer_into_an_honest_handoff(self) -> None:
        _ledger, trajectories, _adapters = self._tight_campaign(3_000)
        blocked = [t for t in trajectories if t.outcome == "escalated"]
        assert blocked
        assert {t.checker_verdict for t in blocked} == {CheckerVerdict.UNDETERMINED}
