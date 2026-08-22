from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rote.compiler.derivations import apply_derivation
from rote.compiler.paths import resolve_path
from rote.contracts.canonical import canonical_hash
from rote.contracts.errors import CompilerError
from rote.contracts.plan import ArgBinding, BindingKind, Plan, ReplayOutcome, ValidationReport
from rote.contracts.trajectory import Trajectory


def validate_plan(plan: Plan, holdout: Sequence[Trajectory]) -> ValidationReport:
    if not plan.steps:
        raise CompilerError("a plan with no steps cannot be replay-validated")
    outcomes = tuple(replay_plan(plan, trajectory) for trajectory in holdout)
    return ValidationReport(
        holdout_size=len(holdout),
        path_equal=sum(1 for outcome in outcomes if outcome.path_equal),
        playback_misses=sum(1 for outcome in outcomes if outcome.playback_miss),
        outcomes=outcomes,
    )


# tools are replaced by the recording; nothing else is, so a divergence is the plan's fault
def replay_plan(plan: Plan, trajectory: Trajectory) -> ReplayOutcome:
    recorded = {_call_key(step.tool, step.args): (step.result or {}) for step in trajectory.steps}
    committed: list[dict[str, Any]] = []

    for step in plan.steps:
        resolved: dict[str, Any] = {}
        for binding in step.args:
            found, value = _resolve(binding, trajectory.task_input_redacted, committed)
            if not found:
                return _miss(trajectory, step.index, f"{step.tool}: {binding.arg_name} unresolved")
            resolved[binding.arg_name] = value

        key = _call_key(step.tool, resolved)
        if key not in recorded:
            return _miss(
                trajectory, step.index, f"{step.tool}: no recorded call matches these arguments"
            )
        committed.append(recorded[key])

    return ReplayOutcome(
        trajectory_id=trajectory.trajectory_id,
        path_equal=True,
        playback_miss=False,
        truncated_at=len(plan.steps) if plan.truncated else None,
        detail="every planned step matched a recorded call",
    )


def _resolve(
    binding: ArgBinding, task_input: dict[str, Any], committed: Sequence[dict[str, Any]]
) -> tuple[bool, Any]:
    if binding.kind is BindingKind.LITERAL:
        return True, binding.literal_value
    if binding.kind is BindingKind.FROM_INPUT:
        return resolve_path(str(binding.json_path), task_input)
    if binding.kind is BindingKind.FROM_STEP:
        index = binding.source_step_index
        if index is None or index >= len(committed):
            return False, None
        return resolve_path(str(binding.json_path), committed[index])
    if binding.kind is BindingKind.FROM_DERIVATION:
        return _resolve_derivation(binding, task_input, committed)
    raise CompilerError(f"binding kind {binding.kind} is not executable in v1")


def _resolve_derivation(
    binding: ArgBinding, task_input: dict[str, Any], committed: Sequence[dict[str, Any]]
) -> tuple[bool, Any]:
    derivation = binding.derivation
    if derivation is None:
        return False, None
    values: list[int] = []
    for operand in derivation.operands:
        if operand.kind is BindingKind.FROM_INPUT:
            found, value = resolve_path(operand.json_path, task_input)
        elif operand.source_step_index is None or operand.source_step_index >= len(committed):
            return False, None
        else:
            found, value = resolve_path(operand.json_path, committed[operand.source_step_index])
        if not found or not isinstance(value, int) or isinstance(value, bool):
            return False, None
        values.append(value)
    return True, apply_derivation(derivation.derivation_id, values)


def _call_key(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{canonical_hash(arguments)}"


def _miss(trajectory: Trajectory, index: int, detail: str) -> ReplayOutcome:
    return ReplayOutcome(
        trajectory_id=trajectory.trajectory_id,
        path_equal=False,
        playback_miss=True,
        truncated_at=index,
        detail=detail,
    )
