import ast
import json
import pathlib
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from rote.compiler.builder import build_plan
from rote.compiler.replay import validate_plan
from rote.contracts.canonical import canonical_bytes
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import ExecutorError, PolicyError, RecordNotFoundError
from rote.contracts.execution import (
    EscalationReason,
    ExecutionOutcome,
    ResultVerdict,
)
from rote.contracts.plan import Plan, PlanStatus, PlanStep, PolicyRequirement
from rote.contracts.tools import ToolSpec
from rote.contracts.trajectory import GateVerdict, Trajectory
from rote.runtime.executor import execute_plan
from tests.compiler.builders import build_with_steps

RUNTIME_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "runtime"
POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha", "beta"}), max_per_action={Currency.INR: 50_000}
)


class RecordingToolbox:
    enforces_policy = True

    def __init__(
        self,
        results: Mapping[str, dict[str, Any]],
        fail_on: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self._results = dict(results)
        self._fail_on = fail_on
        self._failure = failure or RecordNotFoundError("nothing there")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(name=name, mutating=False, parameters={}) for name in sorted(self._results)
        )

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(payload)))
        if name == self._fail_on:
            raise self._failure
        return dict(self._results[name])


class AcceptsEveryProposal:
    def check_proposed_action(
        self, step: PlanStep, arguments: dict[str, Any], task_input: dict[str, Any]
    ) -> ResultVerdict:
        del step, arguments, task_input
        return ResultVerdict(passed=True)


class AlwaysReject(AcceptsEveryProposal):
    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> ResultVerdict:
        del result, attempts
        return ResultVerdict(passed=False, reason=f"rejecting {step.tool}")


class RejectFrom(AcceptsEveryProposal):
    def __init__(self, index: int) -> None:
        self._index = index

    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> ResultVerdict:
        del result, attempts
        if step.index >= self._index:
            return ResultVerdict(passed=False, reason=f"rejecting step {step.index}")
        return ResultVerdict(passed=True)


def chained_runs(count: int = 20) -> list[Trajectory]:
    return [
        build_with_steps(
            f"t{i}",
            [
                ("alpha", {"record_id": f"REC-{i}"}, {"line_id": f"BNK-{i}"}),
                ("beta", {"line_id": f"BNK-{i}"}, {"ok": 1}),
            ],
            task_input={"record_id": f"REC-{i}"},
        )
        for i in range(count)
    ]


def active_plan(runs: Sequence[Trajectory] | None = None) -> Plan:
    fit = list(runs or chained_runs())
    plan = build_plan(
        fit,
        domain=Domain.RECONCILIATION,
        category=ExceptionCategory.FEE_MISMATCH,
        policy=POLICY,
    )
    plan = plan.model_copy(update={"validation": validate_plan(plan, fit[:5])})
    return plan.model_copy(update={"status": PlanStatus.ACTIVE, "activated_by": "human:x"})


def toolbox_for(index: int = 99, **overrides: Any) -> RecordingToolbox:
    return RecordingToolbox({"alpha": {"line_id": f"BNK-{index}"}, "beta": {"ok": 1}}, **overrides)


def run(plan: Plan, toolbox: RecordingToolbox, inspector: Any = None, index: int = 99):  # type: ignore[no-untyped-def]
    return execute_plan(
        plan=plan,
        task_input={"record_id": f"REC-{index}"},
        toolbox=toolbox,
        inspector=inspector,
    )


class TestOnlyActivatedPlansExecute:
    def test_an_activated_and_validated_plan_runs(self) -> None:
        result = run(active_plan(), toolbox_for())
        assert result.outcome is ExecutionOutcome.RESOLVED
        assert result.steps_completed == 2

    @pytest.mark.parametrize(
        "status",
        [PlanStatus.DRAFT, PlanStatus.SHADOW, PlanStatus.INACTIVE, PlanStatus.RETIRED],
    )
    def test_any_other_status_is_refused(self, status: PlanStatus) -> None:
        plan = active_plan().model_copy(update={"status": status})
        with pytest.raises(ExecutorError):
            run(plan, toolbox_for())

    def test_an_active_plan_with_no_validation_report_is_refused(self) -> None:
        plan = active_plan().model_copy(update={"validation": None})
        with pytest.raises(ExecutorError):
            run(plan, toolbox_for())

    def test_an_active_plan_whose_validation_failed_is_refused(self) -> None:
        plan = active_plan()
        assert plan.validation is not None
        broken = plan.validation.model_copy(update={"path_equal": 0})
        with pytest.raises(ExecutorError):
            run(plan.model_copy(update={"validation": broken}), toolbox_for())

    def test_a_plan_with_no_steps_is_refused(self) -> None:
        plan = active_plan().model_copy(update={"steps": ()})
        with pytest.raises(ExecutorError):
            run(plan, toolbox_for())


class TestEveryCallGoesThroughTheBoundary:
    def test_each_step_is_invoked_on_the_toolbox(self) -> None:
        toolbox = toolbox_for()
        run(active_plan(), toolbox)
        assert [name for name, _args in toolbox.calls] == ["alpha", "beta"]

    def test_the_executor_never_supplies_an_idempotency_key(self) -> None:
        toolbox = toolbox_for()
        run(active_plan(), toolbox)
        assert all("idempotency_key" not in args for _name, args in toolbox.calls)

    def test_the_executor_never_imports_a_tool_adapter(self) -> None:
        for path in sorted(RUNTIME_PACKAGE.rglob("*.py")):
            assert "domain.tools.adapters" not in path.read_text(encoding="utf-8")

    def test_the_compiled_path_contains_no_model(self) -> None:
        banned = {"openai", "anthropic", "groq", "ollama", "langchain", "langgraph", "sklearn"}
        for path in sorted(RUNTIME_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in banned
                elif isinstance(node, ast.Import):
                    assert not {a.name.split(".")[0] for a in node.names} & banned

    def test_the_executor_asks_the_boundary_for_nothing_it_was_not_offered(self) -> None:
        toolbox = toolbox_for()
        run(active_plan(), toolbox)
        offered = {spec.name for spec in toolbox.available_tools()}
        assert {name for name, _args in toolbox.calls} <= offered


class TestQuarantineBeforeCommit:
    def test_a_rejected_result_stops_the_run(self) -> None:
        result = run(active_plan(), toolbox_for(), AlwaysReject())
        assert result.outcome is ExecutionOutcome.ESCALATED
        assert result.escalation_reason is EscalationReason.RESULT_DIVERGENCE

    def test_a_rejected_result_is_never_committed(self) -> None:
        result = run(active_plan(), toolbox_for(), AlwaysReject())
        assert result.handover is not None
        assert result.handover.state.committed == ()

    def test_a_rejected_result_is_not_readable_by_a_later_binding(self) -> None:
        toolbox = toolbox_for()
        result = run(active_plan(), toolbox, RejectFrom(0))
        assert result.handover is not None
        assert result.handover.state.committed == ()
        # the second step binds FROM_STEP on the first result, so it must never have run
        assert [name for name, _args in toolbox.calls] == ["alpha"]

    def test_an_accepted_result_is_committed_and_usable(self) -> None:
        toolbox = toolbox_for(index=7)
        run(active_plan(), toolbox, index=7)
        assert toolbox.calls[1] == ("beta", {"line_id": "BNK-7"})

    def test_the_diverging_result_is_handed_over_separately_and_labelled(self) -> None:
        result = run(active_plan(), toolbox_for(index=7), AlwaysReject(), index=7)
        assert result.handover is not None
        assert result.handover.untrusted_result == {"line_id": "BNK-7"}

    def test_a_later_rejection_keeps_only_the_earlier_commits(self) -> None:
        result = run(active_plan(), toolbox_for(), RejectFrom(1))
        assert result.handover is not None
        assert len(result.handover.state.committed) == 1
        assert result.handover.step_index == 1

    def test_the_default_inspector_accepts_so_the_shape_is_provable_now(self) -> None:
        assert run(active_plan(), toolbox_for()).outcome is ExecutionOutcome.RESOLVED


class TestStateStaysData:
    def test_the_handover_state_survives_a_json_round_trip(self) -> None:
        result = run(active_plan(), toolbox_for(), RejectFrom(1))
        assert result.handover is not None
        rendered = result.handover.state.model_dump(mode="json")
        assert json.loads(json.dumps(rendered)) == rendered

    def test_the_handover_state_is_canonically_serialisable(self) -> None:
        result = run(active_plan(), toolbox_for(), RejectFrom(1))
        assert result.handover is not None
        assert canonical_bytes(result.handover.state.model_dump(mode="json"))

    def test_the_whole_result_is_canonically_serialisable(self) -> None:
        assert canonical_bytes(run(active_plan(), toolbox_for()).model_dump(mode="json"))

    def test_the_state_carries_the_task_input_it_started_from(self) -> None:
        result = run(active_plan(), toolbox_for(), RejectFrom(1), index=7)
        assert result.handover is not None
        assert result.handover.state.task_input == {"record_id": "REC-7"}


class TestDeterminism:
    def test_twenty_identical_runs_produce_one_outcome_hash(self) -> None:
        plan = active_plan()
        hashes = {run(plan, toolbox_for()).outcome_hash for _ in range(20)}
        assert len(hashes) == 1

    def test_a_different_task_produces_a_different_hash(self) -> None:
        plan = active_plan()
        first = run(plan, toolbox_for(index=1), index=1).outcome_hash
        second = run(plan, toolbox_for(index=2), index=2).outcome_hash
        assert first != second

    def test_an_escalated_run_hashes_differently_from_a_resolved_one(self) -> None:
        plan = active_plan()
        resolved = run(plan, toolbox_for()).outcome_hash
        escalated = run(plan, toolbox_for(), AlwaysReject()).outcome_hash
        assert resolved != escalated

    def test_the_hash_covers_the_calls_that_were_made(self) -> None:
        plan = active_plan()
        full = run(plan, toolbox_for()).outcome_hash
        partial = run(plan, toolbox_for(), RejectFrom(1)).outcome_hash
        assert full != partial


class TestFailuresEscalateSafely:
    def test_a_refusal_at_the_gate_escalates(self) -> None:
        toolbox = toolbox_for(fail_on="beta", failure=PolicyError(GateVerdict.REFUSE, "no"))
        result = run(active_plan(), toolbox)
        assert result.outcome is ExecutionOutcome.ESCALATED
        assert result.escalation_reason is EscalationReason.GATE_NOT_ALLOWLISTED

    def test_a_cap_breach_at_the_gate_escalates(self) -> None:
        toolbox = toolbox_for(fail_on="beta", failure=PolicyError(GateVerdict.ESCALATE, "cap"))
        result = run(active_plan(), toolbox)
        assert result.escalation_reason is EscalationReason.GATE_CAP_EXCEEDED

    def test_a_tool_failure_escalates_rather_than_resolving(self) -> None:
        result = run(active_plan(), toolbox_for(fail_on="beta"))
        assert result.outcome is ExecutionOutcome.ESCALATED
        assert result.escalation_reason is EscalationReason.TOOL_ERROR

    def test_a_failure_never_reports_success(self) -> None:
        for failure in (
            RecordNotFoundError("gone"),
            PolicyError(GateVerdict.REFUSE, "no"),
            PolicyError(GateVerdict.ESCALATE, "cap"),
        ):
            result = run(active_plan(), toolbox_for(fail_on="alpha", failure=failure))
            assert result.outcome is not ExecutionOutcome.RESOLVED

    def test_an_unresolvable_binding_escalates(self) -> None:
        plan = active_plan()
        result = execute_plan(
            plan=plan, task_input={"wrong_field": 1}, toolbox=toolbox_for(), inspector=None
        )
        assert result.escalation_reason is EscalationReason.BINDING_UNRESOLVED

    def test_a_failure_hands_over_the_state_it_had_reached(self) -> None:
        result = run(active_plan(), toolbox_for(fail_on="beta"))
        assert result.handover is not None
        assert len(result.handover.state.committed) == 1

    def test_an_unknown_action_state_is_never_success(self) -> None:
        unknown = PolicyError(GateVerdict.ESCALATE, "the earlier attempt is UNKNOWN")
        result = run(active_plan(), toolbox_for(fail_on="beta", failure=unknown))
        assert result.outcome is ExecutionOutcome.ESCALATED
