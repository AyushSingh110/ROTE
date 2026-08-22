from __future__ import annotations

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.trajectory import Outcome, Trajectory


def matches(
    trajectory: Trajectory,
    *,
    domain: Domain | None,
    outcome: Outcome | None,
    verdict: CheckerVerdict | None,
    agent_model_id: str | None,
) -> bool:
    if domain is not None and trajectory.domain is not domain:
        return False
    if outcome is not None and trajectory.outcome != outcome:
        return False
    if verdict is not None and trajectory.checker_verdict is not verdict:
        return False
    return not (agent_model_id is not None and trajectory.agent_model_id != agent_model_id)
