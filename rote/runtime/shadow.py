from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rote.contracts.canonical import canonical_hash
from rote.contracts.errors import ShadowError
from rote.contracts.execution import (
    EscalationReason,
    ExecutionOutcome,
    ExecutionResult,
    ResultInspector,
    ToolCall,
    outcome_hash,
)
from rote.contracts.plan import Plan
from rote.contracts.shadow import RunAuthority, ShadowDisagreement, ShadowObservation
from rote.contracts.tools import Toolbox, ToolSpec
from rote.contracts.trajectory import Trajectory
from rote.runtime.executor import execute_plan
from rote.safety.gate import IDEMPOTENCY_ARG

DISAGREEMENT_FOR: dict[EscalationReason, ShadowDisagreement] = {
    EscalationReason.BINDING_UNRESOLVED: ShadowDisagreement.BINDING_UNRESOLVED,
    EscalationReason.GATE_NOT_ALLOWLISTED: ShadowDisagreement.POLICY_BLOCKED,
    EscalationReason.GATE_CAP_EXCEEDED: ShadowDisagreement.POLICY_BLOCKED,
    EscalationReason.UNKNOWN_ACTION_STATE: ShadowDisagreement.POLICY_BLOCKED,
    EscalationReason.INVARIANT_VETO: ShadowDisagreement.GUARD_OBJECTED,
    EscalationReason.RESULT_DIVERGENCE: ShadowDisagreement.GUARD_OBJECTED,
    # the only tool behind a shadow boundary is the recording, so a tool error is a miss
    EscalationReason.TOOL_ERROR: ShadowDisagreement.PLAYBACK_MISS,
}


class PlaybackToolbox:
    enforces_policy = False
    mutates_the_world = False

    def __init__(self, *, trajectory: Trajectory, specs: Sequence[ToolSpec]) -> None:
        self._specs = tuple(specs)
        self._recorded = {
            _call_key(step.tool, step.args): dict(step.result)
            for step in trajectory.steps
            if step.result is not None
        }

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        # the gate stamps its own key on the way through; the recording never carried one
        arguments = {key: value for key, value in payload.items() if key != IDEMPOTENCY_ARG}
        recorded = self._recorded.get(_call_key(name, arguments))
        if recorded is None:
            raise ShadowError(f"{name}: the live run never made this call with these arguments")
        return dict(recorded)


def run_shadow(
    *,
    plan: Plan,
    trajectory: Trajectory,
    toolbox: Toolbox,
    mutating_tools: frozenset[str],
    inspector: ResultInspector | None = None,
) -> ShadowObservation:
    result = execute_plan(
        plan=plan,
        task_input=dict(trajectory.task_input_redacted),
        toolbox=toolbox,
        inspector=inspector,
        authority=RunAuthority.SHADOW,
    )
    live_outcome = _live_outcome(trajectory)
    live = live_calls(trajectory)
    live_effect = outcome_hash(live_outcome, _money_moving(live, mutating_tools))
    shadow_effect = outcome_hash(result.outcome, _money_moving(result.calls, mutating_tools))
    effect_equal = live_effect == shadow_effect
    disagreement = _name_the_disagreement(result, live_outcome, effect_equal)
    return ShadowObservation(
        plan_id=plan.plan_id,
        plan_version=plan.version,
        domain=plan.domain,
        category=plan.category,
        trajectory_id=trajectory.trajectory_id,
        agreed=disagreement is ShadowDisagreement.NONE,
        disagreement=disagreement,
        effect_equal=effect_equal,
        path_equal=outcome_hash(live_outcome, live) == result.outcome_hash,
        live_outcome=live_outcome,
        shadow_outcome=result.outcome,
        live_effect_hash=live_effect,
        shadow_effect_hash=shadow_effect,
        shadow_steps_completed=result.steps_completed,
        detail="" if result.handover is None else result.handover.reason,
    )


def live_calls(trajectory: Trajectory) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(tool=step.tool, args=dict(step.args))
        for step in trajectory.steps
        if step.result is not None and step.error is None
    )


# an escalation on either side is a disagreement: a plan that hands off has not done the job,
# and erring this way can only delay a promotion, never grant one
def _name_the_disagreement(
    result: ExecutionResult, live_outcome: ExecutionOutcome, effect_equal: bool
) -> ShadowDisagreement:
    if result.escalation_reason is not None:
        return DISAGREEMENT_FOR[result.escalation_reason]
    if result.outcome is not live_outcome:
        return ShadowDisagreement.OUTCOME_DIFFERS
    if not effect_equal:
        return ShadowDisagreement.EFFECT_DIFFERS
    return ShadowDisagreement.NONE


def _live_outcome(trajectory: Trajectory) -> ExecutionOutcome:
    if trajectory.outcome == "resolved":
        return ExecutionOutcome.RESOLVED
    return ExecutionOutcome.ESCALATED


def _money_moving(
    calls: Sequence[ToolCall], mutating_tools: frozenset[str]
) -> tuple[ToolCall, ...]:
    return tuple(call for call in calls if call.tool in mutating_tools)


def _call_key(tool: str, arguments: Mapping[str, Any]) -> str:
    return f"{tool}:{canonical_hash(dict(arguments))}"


__all__ = ["PlaybackToolbox", "live_calls", "run_shadow"]
