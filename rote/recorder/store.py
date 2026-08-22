from __future__ import annotations

from rote.contracts.errors import RecorderError
from rote.contracts.trajectory import Trajectory


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
