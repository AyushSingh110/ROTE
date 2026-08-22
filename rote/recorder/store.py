from __future__ import annotations

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import RecorderError
from rote.contracts.trajectory import Outcome, Trajectory
from rote.recorder.filters import matches


class InMemoryTrajectoryStore:
    def __init__(self) -> None:
        self._trajectories: list[Trajectory] = []
        self._seen: set[str] = set()

    def append(self, trajectory: Trajectory) -> None:
        key = str(trajectory.trajectory_id)
        if key in self._seen:
            raise RecorderError(f"trajectory {key} is already stored")
        self._seen.add(key)
        self._trajectories.append(trajectory)

    def all(self) -> tuple[Trajectory, ...]:
        return tuple(self._trajectories)

    def count(self) -> int:
        return len(self._trajectories)

    def select(
        self,
        *,
        domain: Domain | None = None,
        outcome: Outcome | None = None,
        verdict: CheckerVerdict | None = None,
        agent_model_id: str | None = None,
    ) -> tuple[Trajectory, ...]:
        return tuple(
            trajectory
            for trajectory in self._trajectories
            if matches(
                trajectory,
                domain=domain,
                outcome=outcome,
                verdict=verdict,
                agent_model_id=agent_model_id,
            )
        )
