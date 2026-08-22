from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import CompilerError
from rote.contracts.trajectory import Trajectory

HASH_SPACE = 1 << 32


class RejectionReason(StrEnum):
    WRONG_DOMAIN = "wrong_domain"
    NOT_RESOLVED = "not_resolved"
    UNLABELLED = "unlabelled"
    NOT_VERIFIED = "not_verified"
    NO_STEPS = "no_steps"


class EligibilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    examined: int
    eligible: int
    rejected: dict[RejectionReason, int]


class Split(NamedTuple):
    fit: tuple[Trajectory, ...]
    holdout: tuple[Trajectory, ...]


def select_eligible(
    trajectories: Sequence[Trajectory], *, domain: Domain
) -> tuple[tuple[Trajectory, ...], EligibilityReport]:
    chosen: list[Trajectory] = []
    rejected: dict[RejectionReason, int] = {}
    for trajectory in trajectories:
        reason = _rejection_reason(trajectory, domain)
        if reason is None:
            chosen.append(trajectory)
            continue
        rejected[reason] = rejected.get(reason, 0) + 1
    return tuple(chosen), EligibilityReport(
        examined=len(trajectories),
        eligible=len(chosen),
        rejected=dict(sorted(rejected.items())),
    )


def _rejection_reason(trajectory: Trajectory, domain: Domain) -> RejectionReason | None:
    if trajectory.domain is not domain:
        return RejectionReason.WRONG_DOMAIN
    if trajectory.outcome != "resolved":
        return RejectionReason.NOT_RESOLVED
    if trajectory.checker_verdict is None:
        return RejectionReason.UNLABELLED
    if trajectory.checker_verdict is not CheckerVerdict.PASS:
        return RejectionReason.NOT_VERIFIED
    if not trajectory.steps:
        return RejectionReason.NO_STEPS
    return None


# hashed rather than seeded, so the same trajectory lands in the same half on every re-run
def hash_split(trajectories: Sequence[Trajectory], *, holdout_fraction: float) -> Split:
    if not 0.0 <= holdout_fraction <= 1.0:
        raise CompilerError(f"holdout_fraction {holdout_fraction} is not between 0 and 1")
    cutoff = holdout_fraction * HASH_SPACE
    fit: list[Trajectory] = []
    holdout: list[Trajectory] = []
    for trajectory in trajectories:
        bucket = _bucket(str(trajectory.trajectory_id))
        (holdout if bucket < cutoff else fit).append(trajectory)
    return Split(fit=tuple(fit), holdout=tuple(holdout))


def _bucket(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
