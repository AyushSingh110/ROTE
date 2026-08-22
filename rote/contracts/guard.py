from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rote.contracts.execution import ResultVerdict

FROZEN = ConfigDict(extra="forbid", frozen=True)
FULL_SCALE = 1000


class GuardSignal(StrEnum):
    STRUCTURAL = "structural"
    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    BEHAVIOURAL = "behavioural"
    INVARIANT = "invariant"


# scored per mille, never as floats, so a verdict stays canonically comparable across runs
class SignalScore(BaseModel):
    model_config = FROZEN

    signal: GuardSignal
    score_per_mille: int = Field(ge=0, le=FULL_SCALE)
    detail: str = ""


class GuardWeights(BaseModel):
    model_config = FROZEN

    structural: int = Field(ge=0, le=FULL_SCALE)
    numeric: int = Field(ge=0, le=FULL_SCALE)
    categorical: int = Field(ge=0, le=FULL_SCALE)
    behavioural: int = Field(ge=0, le=FULL_SCALE)

    @model_validator(mode="after")
    def _require_a_full_scale(self) -> GuardWeights:
        total = self.structural + self.numeric + self.categorical + self.behavioural
        if total != FULL_SCALE:
            raise ValueError(f"guard weights must sum to {FULL_SCALE}, got {total}")
        return self

    def weight_for(self, signal: GuardSignal) -> int:
        return {
            GuardSignal.STRUCTURAL: self.structural,
            GuardSignal.NUMERIC: self.numeric,
            GuardSignal.CATEGORICAL: self.categorical,
            GuardSignal.BEHAVIOURAL: self.behavioural,
        }.get(signal, 0)


class GuardConfig(BaseModel):
    model_config = FROZEN

    weights: GuardWeights
    threshold_per_mille: int = Field(ge=0, le=FULL_SCALE)
    retry_penalty_per_mille: int = Field(ge=0, le=FULL_SCALE)
    added_key_penalty_per_mille: int = Field(ge=0, le=FULL_SCALE)


class GuardVerdict(ResultVerdict):
    model_config = FROZEN

    step_index: int = Field(ge=0)
    checkpoint: str
    divergence_per_mille: int = Field(ge=0, le=FULL_SCALE)
    threshold_per_mille: int = Field(ge=0, le=FULL_SCALE)
    # the raw vector is always carried, so the Phase 14 sweep is offline arithmetic on stored data
    scores: tuple[SignalScore, ...]
    failed_invariants: tuple[str, ...] = ()
    vetoed: bool = False

    def score_for(self, signal: GuardSignal) -> int:
        for score in self.scores:
            if score.signal is signal:
                return score.score_per_mille
        return 0
