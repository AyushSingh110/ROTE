from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from rote.contracts.trajectory import Trajectory


class SequenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: tuple[str, ...]
    count: int


def tool_sequence(trajectory: Trajectory) -> tuple[str, ...]:
    return tuple(step.tool for step in trajectory.steps)


# a failed call changed nothing and a repeated call is a retry, so neither belongs in a skeleton
def collapsed_sequence(trajectory: Trajectory) -> tuple[str, ...]:
    collapsed: list[str] = []
    for step in trajectory.steps:
        if step.error is not None:
            continue
        if collapsed and collapsed[-1] == step.tool:
            continue
        collapsed.append(step.tool)
    return tuple(collapsed)


def sequence_of(trajectory: Trajectory, *, collapse: bool) -> tuple[str, ...]:
    return collapsed_sequence(trajectory) if collapse else tool_sequence(trajectory)


def group_by_sequence(
    trajectories: Sequence[Trajectory], *, collapse: bool
) -> tuple[SequenceGroup, ...]:
    counts = Counter(sequence_of(trajectory, collapse=collapse) for trajectory in trajectories)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(SequenceGroup(sequence=sequence, count=count) for sequence, count in ordered)
