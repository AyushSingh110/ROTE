from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.execution import ExecutionOutcome

FROZEN = ConfigDict(extra="forbid", frozen=True)


# two levels and no more: SHADOW is a lesser authority, never a way past a status a plan
# has not earned. The status each one requires lives in the executor.
class RunAuthority(StrEnum):
    ACTIVE = "active"
    SHADOW = "shadow"


class ShadowDisagreement(StrEnum):
    NONE = "none"
    OUTCOME_DIFFERS = "outcome_differs"
    EFFECT_DIFFERS = "effect_differs"
    PLAYBACK_MISS = "playback_miss"
    POLICY_BLOCKED = "policy_blocked"
    GUARD_OBJECTED = "guard_objected"
    BINDING_UNRESOLVED = "binding_unresolved"


class ShadowObservation(BaseModel):
    model_config = FROZEN

    plan_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    domain: Domain
    category: ExceptionCategory
    trajectory_id: UUID
    agreed: bool
    disagreement: ShadowDisagreement
    # effect is the money-moving calls only; path is every call including reads
    effect_equal: bool
    path_equal: bool
    live_outcome: ExecutionOutcome
    shadow_outcome: ExecutionOutcome
    live_effect_hash: str = Field(min_length=1)
    shadow_effect_hash: str = Field(min_length=1)
    shadow_steps_completed: int = Field(ge=0)
    detail: str = ""

    @model_validator(mode="after")
    def _agreement_and_reason_cannot_contradict(self) -> ShadowObservation:
        if self.agreed is not (self.disagreement is ShadowDisagreement.NONE):
            raise ValueError("an observation agrees exactly when it names no disagreement")
        return self


__all__ = ["RunAuthority", "ShadowDisagreement", "ShadowObservation"]
