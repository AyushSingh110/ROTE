from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rote.compiler.binding import infer_binding
from rote.compiler.expectations import learn_expectation
from rote.compiler.sequences import group_by_sequence, tool_sequence
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.plan import (
    ArgBinding,
    Plan,
    PlanStatus,
    PlanStep,
    PolicyRequirement,
    TruncationReason,
)
from rote.contracts.trajectory import Trajectory

COMPILER_VERSION = "compiler-1"


def build_plan(
    trajectories: Sequence[Trajectory],
    *,
    domain: Domain,
    category: ExceptionCategory,
    policy: PolicyRequirement,
    version: int = 1,
) -> Plan:
    if not trajectories:
        raise CompilerError("a plan cannot be compiled from no trajectories")
    model_id = _single_model(trajectories)

    skeleton = group_by_sequence(trajectories, collapse=False)[0].sequence
    group = [t for t in trajectories if tool_sequence(t) == skeleton]

    steps, truncation = _compile_steps(skeleton, group)
    return Plan(
        plan_id=f"{domain.value}:{category.value}",
        version=version,
        domain=domain,
        category=category,
        steps=tuple(steps),
        policy=policy,
        status=PlanStatus.DRAFT,
        built_from=tuple(t.trajectory_id for t in group),
        compiler_version=COMPILER_VERSION,
        agent_model_id=model_id,
        skeleton=skeleton,
        truncated=truncation is not None,
        truncation_reason=truncation,
        coverage_count=len(group),
        coverage_total=len(trajectories),
        validation=None,
    )


def _compile_steps(
    skeleton: Sequence[str], group: Sequence[Trajectory]
) -> tuple[list[PlanStep], TruncationReason | None]:
    task_inputs = [t.task_input_redacted for t in group]
    steps: list[PlanStep] = []

    for index, tool in enumerate(skeleton):
        names = _argument_names(group, index)
        if names is None:
            return steps, TruncationReason.INCONSISTENT_ARGUMENTS

        prior = [[_result(t, earlier) for earlier in range(index)] for t in group]
        bindings: list[ArgBinding] = []
        for name in names:
            observed = [t.steps[index].args[name] for t in group]
            binding = infer_binding(name, observed, task_inputs, prior)
            if binding is None:
                return steps, TruncationReason.UNBOUND_ARGUMENT
            bindings.append(binding)

        steps.append(
            PlanStep(
                index=index,
                kind="TOOL_CALL",
                tool=tool,
                args=tuple(bindings),
                expect=learn_expectation([_result(t, index) for t in group]),
            )
        )
    return steps, None


def _argument_names(group: Sequence[Trajectory], index: int) -> list[str] | None:
    names = sorted(group[0].steps[index].args)
    for trajectory in group[1:]:
        if sorted(trajectory.steps[index].args) != names:
            return None
    return names


def _result(trajectory: Trajectory, index: int) -> dict[str, Any]:
    return trajectory.steps[index].result or {}


def _single_model(trajectories: Sequence[Trajectory]) -> str:
    models = sorted({t.agent_model_id for t in trajectories})
    if len(models) > 1:
        raise CompilerError(f"a plan may not be compiled across models: {models}")
    return models[0]
