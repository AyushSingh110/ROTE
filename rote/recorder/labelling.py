from __future__ import annotations

from rote.contracts.errors import RecorderError
from rote.contracts.reconciliation import GroundTruth, ReconciliationFacts, WorldSnapshot
from rote.contracts.trajectory import Trajectory
from rote.domain.checkers.reconciliation import check_outcome


def label_trajectory(
    trajectory: Trajectory,
    *,
    facts: ReconciliationFacts,
    ground_truth: GroundTruth,
    world: WorldSnapshot,
) -> Trajectory:
    if trajectory.checker_verdict is not None:
        raise RecorderError(f"trajectory {trajectory.trajectory_id} already carries a verdict")
    result = check_outcome(facts, ground_truth, world)
    return trajectory.model_copy(
        update={"checker_verdict": result.verdict, "checker_version": result.checker_version}
    )
