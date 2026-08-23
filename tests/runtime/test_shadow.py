import pathlib
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from rote.compiler.builder import build_plan
from rote.compiler.replay import validate_plan
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import ExecutorError, ShadowError
from rote.contracts.execution import EscalationReason, ExecutionOutcome, ResultVerdict
from rote.contracts.plan import Plan, PlanStatus, PlanStep, PolicyRequirement
from rote.contracts.policy import (
    ExecutionPath,
    PolicyConfig,
    PolicyContext,
    PolicyRule,
)
from rote.contracts.shadow import RunAuthority, ShadowDisagreement, ShadowObservation
from rote.contracts.tools import ToolSpec
from rote.contracts.trajectory import Outcome, Trajectory
from rote.runtime.executor import REQUIRED_STATUS, execute_plan
from rote.runtime.shadow import DISAGREEMENT_FOR, PlaybackToolbox, run_shadow
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from tests.compiler.builders import build_with_steps

SHADOW_MODULE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "runtime" / "shadow.py"
MUTATING = frozenset({"beta", "delta"})
SPECS = (
    ToolSpec(name="alpha", mutating=False, parameters={}),
    ToolSpec(name="beta", mutating=True, parameters={}),
    ToolSpec(name="gamma", mutating=False, parameters={}),
    ToolSpec(name="delta", mutating=True, parameters={}),
)
POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha", "beta"}), max_per_action={Currency.INR: 50_000}
)


def ticks() -> Iterator[datetime]:
    moment = datetime(2026, 8, 23, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(seconds=1)


Step = tuple[str, dict[str, object], dict[str, object]]


def two_step(index: int) -> list[Step]:
    return [
        ("alpha", {"record_id": f"REC-{index}"}, {"line_id": f"BNK-{index}"}),
        ("beta", {"line_id": f"BNK-{index}"}, {"ok": 1}),
    ]


def recording(
    name: str,
    steps: Sequence[Step] | None = None,
    *,
    index: int = 99,
    task_input: dict[str, object] | None = None,
    outcome: Outcome = "resolved",
) -> Trajectory:
    built = build_with_steps(
        name,
        list(steps if steps is not None else two_step(index)),
        task_input=task_input if task_input is not None else {"record_id": f"REC-{index}"},
    )
    return built.model_copy(update={"outcome": outcome})


def shadow_plan(status: PlanStatus = PlanStatus.SHADOW) -> Plan:
    fit = [recording(f"t{i}", index=i) for i in range(20)]
    plan = build_plan(
        fit, domain=Domain.RECONCILIATION, category=ExceptionCategory.FEE_MISMATCH, policy=POLICY
    )
    plan = plan.model_copy(update={"validation": validate_plan(plan, fit[:5])})
    return plan.model_copy(update={"status": status})


def playback(trajectory: Trajectory) -> PlaybackToolbox:
    return PlaybackToolbox(trajectory=trajectory, specs=SPECS)


def observe(
    trajectory: Trajectory,
    *,
    plan: Plan | None = None,
    toolbox: Any = None,
    inspector: Any = None,
) -> ShadowObservation:
    return run_shadow(
        plan=plan or shadow_plan(),
        trajectory=trajectory,
        toolbox=toolbox if toolbox is not None else playback(trajectory),
        mutating_tools=MUTATING,
        inspector=inspector,
    )


def gated(playback_toolbox: Any, allowed: frozenset[str] = frozenset({"alpha", "beta"})) -> Any:
    clock = ticks()
    config = PolicyConfig(
        rules=(
            PolicyRule(
                path=ExecutionPath.COMPILED_PLAN,
                category=None,
                allowed_tools=allowed,
                max_per_action={Currency.INR: 50_000},
                max_per_window={Currency.INR: 500_000},
                window_seconds=3_600,
            ),
        ),
        money_arguments=(),
        require_idempotency_for=frozenset({"beta"}),
    )
    gate = PolicyGate(
        adapters=playback_toolbox,
        config=config,
        ledger=Ledger(),
        clock=lambda: next(clock),
    )
    return gate.for_task(
        PolicyContext(
            task_id="task-1",
            correlation_id="task-1:shadow",
            path=ExecutionPath.COMPILED_PLAN,
            category=None,
            actor="system:shadow",
        )
    )


class RejectsEverySuggestion:
    def check_proposed_action(
        self, step: PlanStep, arguments: dict[str, Any], task_input: dict[str, Any]
    ) -> ResultVerdict:
        del arguments, task_input
        return ResultVerdict(passed=False, reason=f"vetoing {step.tool}")

    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> ResultVerdict:
        del step, result, attempts
        return ResultVerdict(passed=True)


class RejectsEveryResult:
    def check_proposed_action(
        self, step: PlanStep, arguments: dict[str, Any], task_input: dict[str, Any]
    ) -> ResultVerdict:
        del step, arguments, task_input
        return ResultVerdict(passed=True)

    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> ResultVerdict:
        del result, attempts
        return ResultVerdict(passed=False, reason=f"diverged at {step.tool}")


class UndeclaredBoundary:
    enforces_policy = True

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return SPECS

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        del name, payload
        return {"ok": 1}


class AdmitsItCanAct(UndeclaredBoundary):
    mutates_the_world = True


class TestShadowAuthorityGrantsNothing:
    def test_there_are_exactly_two_authorities(self) -> None:
        assert set(REQUIRED_STATUS) == set(RunAuthority)
        assert len(REQUIRED_STATUS) == 2

    def test_no_authority_lets_an_unearned_status_run(self) -> None:
        earned = {PlanStatus.ACTIVE, PlanStatus.SHADOW}
        assert set(REQUIRED_STATUS.values()) == earned

    @pytest.mark.parametrize(
        "status",
        [PlanStatus.DRAFT, PlanStatus.ACTIVE, PlanStatus.INACTIVE, PlanStatus.RETIRED],
    )
    def test_only_a_shadowing_plan_runs_under_shadow_authority(self, status: PlanStatus) -> None:
        trajectory = recording("case")
        with pytest.raises(ExecutorError):
            execute_plan(
                plan=shadow_plan(status),
                task_input=dict(trajectory.task_input_redacted),
                toolbox=playback(trajectory),
                authority=RunAuthority.SHADOW,
            )

    def test_a_shadowing_plan_still_cannot_run_under_active_authority(self) -> None:
        trajectory = recording("case")
        with pytest.raises(ExecutorError):
            execute_plan(
                plan=shadow_plan(),
                task_input=dict(trajectory.task_input_redacted),
                toolbox=playback(trajectory),
            )

    def test_a_shadowing_plan_without_a_passing_validation_is_refused(self) -> None:
        trajectory = recording("case")
        plan = shadow_plan()
        assert plan.validation is not None
        broken = plan.validation.model_copy(update={"path_equal": 0})
        with pytest.raises(ExecutorError):
            execute_plan(
                plan=plan.model_copy(update={"validation": broken}),
                task_input=dict(trajectory.task_input_redacted),
                toolbox=playback(trajectory),
                authority=RunAuthority.SHADOW,
            )


class TestAShadowRunCannotReachTheWorld:
    def test_a_boundary_that_can_act_is_refused(self) -> None:
        trajectory = recording("case")
        with pytest.raises(ExecutorError):
            execute_plan(
                plan=shadow_plan(),
                task_input=dict(trajectory.task_input_redacted),
                toolbox=AdmitsItCanAct(),
                authority=RunAuthority.SHADOW,
            )

    # absence of the declaration means "it can act": a boundary must earn the shadow run
    def test_a_boundary_that_declares_nothing_is_refused(self) -> None:
        trajectory = recording("case")
        with pytest.raises(ExecutorError):
            execute_plan(
                plan=shadow_plan(),
                task_input=dict(trajectory.task_input_redacted),
                toolbox=UndeclaredBoundary(),
                authority=RunAuthority.SHADOW,
            )

    def test_the_playback_boundary_declares_that_it_cannot_act(self) -> None:
        assert playback(recording("case")).mutates_the_world is False

    def test_a_gate_forwards_the_declaration_of_whatever_sits_behind_it(self) -> None:
        assert gated(playback(recording("case"))).mutates_the_world is False
        assert gated(AdmitsItCanAct()).mutates_the_world is True

    def test_the_shadow_runner_holds_no_tool_adapter(self) -> None:
        source = SHADOW_MODULE.read_text(encoding="utf-8")
        assert "adapters" not in source
        assert "World" not in source

    # the runner reports evidence; only the registry may act on it
    def test_the_shadow_runner_cannot_promote_anything(self) -> None:
        source = SHADOW_MODULE.read_text(encoding="utf-8")
        assert "activate" not in source
        assert "registry" not in source.lower()


class TestPlaybackReturnsTheRecordingAndNothingElse:
    def test_a_matching_call_returns_the_recorded_result(self) -> None:
        toolbox = playback(recording("case", index=7))
        assert toolbox.invoke("alpha", {"record_id": "REC-7"}) == {"line_id": "BNK-7"}

    def test_an_unrecorded_call_raises_rather_than_inventing_a_result(self) -> None:
        toolbox = playback(recording("case", index=7))
        with pytest.raises(ShadowError):
            toolbox.invoke("alpha", {"record_id": "REC-8"})

    # the gate adds the key on its way through, and the recording never had one
    def test_the_gate_derived_idempotency_key_is_ignored_when_matching(self) -> None:
        toolbox = playback(recording("case", index=7))
        assert toolbox.invoke("beta", {"line_id": "BNK-7", "idempotency_key": "k"}) == {"ok": 1}

    def test_the_offered_tools_are_the_ones_it_was_given(self) -> None:
        assert playback(recording("case")).available_tools() == SPECS


class TestAgreementIsMeasuredOnEffect:
    def test_the_same_actions_agree(self) -> None:
        observation = observe(recording("case"))
        assert observation.agreed is True
        assert observation.disagreement is ShadowDisagreement.NONE
        assert observation.effect_equal is True
        assert observation.path_equal is True

    def test_an_extra_read_by_the_live_agent_is_not_a_disagreement(self) -> None:
        steps = two_step(99)
        steps.insert(1, ("gamma", {"record_id": "REC-99"}, {"noted": True}))
        observation = observe(recording("case", steps))
        assert observation.agreed is True
        assert observation.effect_equal is True
        assert observation.path_equal is False

    def test_a_money_movement_the_plan_would_have_skipped_disagrees(self) -> None:
        steps = two_step(99)
        steps.append(("delta", {"line_id": "BNK-99", "minor_units": 500}, {"ok": 1}))
        observation = observe(recording("case", steps))
        assert observation.agreed is False
        assert observation.disagreement is ShadowDisagreement.EFFECT_DIFFERS
        assert observation.effect_equal is False

    # an argument the recording cannot match is a miss before it can be an effect difference,
    # because the plan never gets a result to continue from
    def test_a_differing_argument_is_reported_as_a_miss_not_an_effect_difference(self) -> None:
        steps = two_step(99)
        steps[1] = ("beta", {"line_id": "BNK-99", "minor_units": 500}, {"ok": 1})
        observation = observe(recording("case", steps))
        assert observation.disagreement is ShadowDisagreement.PLAYBACK_MISS
        assert "beta" in observation.detail

    def test_a_live_escalation_against_a_shadow_resolution_disagrees(self) -> None:
        observation = observe(recording("case", outcome="escalated"))
        assert observation.agreed is False
        assert observation.disagreement is ShadowDisagreement.OUTCOME_DIFFERS

    def test_an_observation_cannot_agree_and_name_a_disagreement(self) -> None:
        fields = observe(recording("case")).model_dump()
        fields["disagreement"] = ShadowDisagreement.EFFECT_DIFFERS
        with pytest.raises(ValidationError, match="agrees"):
            ShadowObservation.model_validate(fields)


class TestEveryDisagreementIsNamed:
    def test_every_way_a_run_can_escalate_has_a_shadow_name(self) -> None:
        assert set(DISAGREEMENT_FOR) == set(EscalationReason)

    def test_no_escalation_is_ever_filed_as_agreement(self) -> None:
        assert ShadowDisagreement.NONE not in set(DISAGREEMENT_FOR.values())

    def test_a_call_the_live_run_never_made_is_a_playback_miss(self) -> None:
        trajectory = recording("case", two_step(1), task_input={"record_id": "REC-2"})
        observation = observe(trajectory)
        assert observation.disagreement is ShadowDisagreement.PLAYBACK_MISS
        assert observation.shadow_outcome is ExecutionOutcome.ESCALATED

    def test_an_argument_this_run_cannot_supply_is_named_as_such(self) -> None:
        trajectory = recording("case", two_step(1), task_input={"other": "REC-2"})
        assert observe(trajectory).disagreement is ShadowDisagreement.BINDING_UNRESOLVED

    def test_a_tool_the_policy_withholds_is_named_as_a_policy_block(self) -> None:
        trajectory = recording("case")
        observation = observe(trajectory, toolbox=gated(playback(trajectory), frozenset({"alpha"})))
        assert observation.disagreement is ShadowDisagreement.POLICY_BLOCKED

    def test_a_vetoed_proposal_is_named_as_a_guard_objection(self) -> None:
        observation = observe(recording("case"), inspector=RejectsEverySuggestion())
        assert observation.disagreement is ShadowDisagreement.GUARD_OBJECTED

    def test_a_rejected_result_is_named_as_a_guard_objection(self) -> None:
        observation = observe(recording("case"), inspector=RejectsEveryResult())
        assert observation.disagreement is ShadowDisagreement.GUARD_OBJECTED

    def test_a_disagreement_never_carries_the_agreed_flag(self) -> None:
        observation = observe(recording("case", outcome="escalated"))
        assert observation.agreed is (observation.disagreement is ShadowDisagreement.NONE)


class TestTheShadowRunPassesThroughTheRealGate:
    def test_the_full_gate_sits_in_front_of_the_recording(self) -> None:
        trajectory = recording("case")
        observation = observe(trajectory, toolbox=gated(playback(trajectory)))
        assert observation.agreed is True

    def test_the_shadow_run_is_recorded_against_the_trajectory_it_shadowed(self) -> None:
        trajectory = recording("case")
        observation = observe(trajectory)
        assert observation.trajectory_id == trajectory.trajectory_id
        assert observation.plan_id == shadow_plan().plan_id
        assert observation.category is ExceptionCategory.FEE_MISMATCH
