from __future__ import annotations

from typing import Any

from rote.contracts.errors import ExecutorError, PolicyError, RoteError
from rote.contracts.execution import (
    EscalationReason,
    ExecutionOutcome,
    ExecutionResult,
    ExecutionState,
    Handover,
    ResultInspector,
    ResultVerdict,
    ToolCall,
    outcome_hash,
)
from rote.contracts.plan import ArgBinding, BindingKind, Plan, PlanStatus, PlanStep
from rote.contracts.tools import Toolbox
from rote.contracts.trajectory import GateVerdict
from rote.runtime.bindings import resolve_binding

GATE_REASONS: dict[GateVerdict, EscalationReason] = {
    GateVerdict.REFUSE: EscalationReason.GATE_NOT_ALLOWLISTED,
    GateVerdict.ESCALATE: EscalationReason.GATE_CAP_EXCEEDED,
}


class AcceptEveryResult:
    def inspect(self, step: PlanStep, result: dict[str, Any]) -> ResultVerdict:
        del step, result
        return ResultVerdict(passed=True)


def execute_plan(
    *,
    plan: Plan,
    task_input: dict[str, Any],
    toolbox: Toolbox,
    inspector: ResultInspector | None = None,
) -> ExecutionResult:
    _require_permission_to_run(plan)
    check = inspector or AcceptEveryResult()
    state = ExecutionState(task_input=dict(task_input), committed=())
    calls: list[ToolCall] = []

    for step in plan.steps:
        resolved = _resolve_arguments(step, state)
        if resolved is None:
            return _escalate(plan, calls, EscalationReason.BINDING_UNRESOLVED, state, step, None)

        try:
            result = toolbox.invoke(step.tool, resolved)
        except PolicyError as error:
            reason = GATE_REASONS.get(
                error.verdict if isinstance(error.verdict, GateVerdict) else GateVerdict.REFUSE,
                EscalationReason.GATE_NOT_ALLOWLISTED,
            )
            return _escalate(plan, calls, reason, state, step, None)
        except RoteError:
            return _escalate(plan, calls, EscalationReason.TOOL_ERROR, state, step, None)

        calls.append(ToolCall(tool=step.tool, args=resolved))
        # the result is pending here: it is not state until the inspector lets it become state
        verdict = check.inspect(step, result)
        if not verdict.passed:
            return _escalate(
                plan, calls, EscalationReason.RESULT_DIVERGENCE, state, step, result, verdict.reason
            )
        state = state.with_committed(result)

    return ExecutionResult(
        plan_id=plan.plan_id,
        plan_version=plan.version,
        outcome=ExecutionOutcome.RESOLVED,
        escalation_reason=None,
        steps_completed=len(plan.steps),
        calls=tuple(calls),
        handover=None,
        outcome_hash=outcome_hash(ExecutionOutcome.RESOLVED, calls),
    )


def _require_permission_to_run(plan: Plan) -> None:
    if plan.status is not PlanStatus.ACTIVE:
        raise ExecutorError(f"{plan.plan_id} is {plan.status.value}, only an active plan may run")
    if plan.validation is None or not plan.validation.passed:
        raise ExecutorError(f"{plan.plan_id} has no passing validation report")
    if not plan.steps:
        raise ExecutorError(f"{plan.plan_id} has no steps to run")


def _resolve_arguments(step: PlanStep, state: ExecutionState) -> dict[str, Any] | None:
    resolved: dict[str, Any] = {}
    for binding in step.args:
        found, value = resolve_binding(binding, state.task_input, state.committed)
        if not found:
            return None
        resolved[binding.arg_name] = value
    return resolved


def _escalate(
    plan: Plan,
    calls: list[ToolCall],
    reason: EscalationReason,
    state: ExecutionState,
    step: PlanStep,
    untrusted: dict[str, Any] | None,
    detail: str = "",
) -> ExecutionResult:
    return ExecutionResult(
        plan_id=plan.plan_id,
        plan_version=plan.version,
        outcome=ExecutionOutcome.ESCALATED,
        escalation_reason=reason,
        steps_completed=len(state.committed),
        calls=tuple(calls),
        handover=Handover(
            step_index=step.index,
            reason=detail or reason.value,
            state=state,
            untrusted_result=untrusted,
        ),
        outcome_hash=outcome_hash(ExecutionOutcome.ESCALATED, calls),
    )


__all__ = ["AcceptEveryResult", "ArgBinding", "BindingKind", "execute_plan"]
