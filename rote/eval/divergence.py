from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.guard import FULL_SCALE, GuardSignal, GuardWeights, SignalScore
from rote.domain.generators.divergence import DivergenceLabel

FROZEN = ConfigDict(extra="forbid", frozen=True)
DEFAULT_STEP_PER_MILLE = 50


class ScoredCase(BaseModel):
    model_config = FROZEN

    label: DivergenceLabel
    applied: bool
    scores: tuple[SignalScore, ...]

    def divergence_under(self, weights: GuardWeights) -> int:
        return (
            sum(
                score.score_per_mille * weights.weight_for(score.signal)
                for score in self.scores
                if score.signal is not GuardSignal.INVARIANT
            )
            // FULL_SCALE
        )


class CurvePoint(BaseModel):
    model_config = FROZEN

    threshold_per_mille: int = Field(ge=0, le=FULL_SCALE)
    divergences: int = Field(ge=0)
    missed: int = Field(ge=0)
    clean: int = Field(ge=0)
    false_aborts: int = Field(ge=0)
    missed_per_mille: int = Field(ge=0, le=FULL_SCALE)
    false_abort_per_mille: int = Field(ge=0, le=FULL_SCALE)


class DivergenceCurve(BaseModel):
    model_config = FROZEN

    weights: GuardWeights
    step_per_mille: int = Field(gt=0)
    points: tuple[CurvePoint, ...]


# the sweep reads stored score vectors, so it is offline arithmetic and never a system re-run
def sweep(
    cases: Sequence[ScoredCase],
    *,
    weights: GuardWeights,
    step_per_mille: int = DEFAULT_STEP_PER_MILLE,
) -> DivergenceCurve:
    divergent = [case for case in cases if case.applied]
    clean = [case for case in cases if case.label is DivergenceLabel.NONE]
    scored_divergent = [case.divergence_under(weights) for case in divergent]
    scored_clean = [case.divergence_under(weights) for case in clean]

    points = []
    for threshold in range(0, FULL_SCALE + 1, step_per_mille):
        missed = sum(1 for value in scored_divergent if value < threshold)
        false_aborts = sum(1 for value in scored_clean if value >= threshold)
        points.append(
            CurvePoint(
                threshold_per_mille=threshold,
                divergences=len(divergent),
                missed=missed,
                clean=len(clean),
                false_aborts=false_aborts,
                missed_per_mille=_rate(missed, len(divergent)),
                false_abort_per_mille=_rate(false_aborts, len(clean)),
            )
        )
    return DivergenceCurve(weights=weights, step_per_mille=step_per_mille, points=tuple(points))


# stated before any data was looked at: subject to the false-abort budget, fewest misses wins;
# ties go to the HIGHER threshold, because a less sensitive guard is cheaper to operate
def select_operating_point(
    curve: DivergenceCurve, *, max_false_abort_per_mille: int
) -> CurvePoint | None:
    affordable = [
        point for point in curve.points if point.false_abort_per_mille <= max_false_abort_per_mille
    ]
    if not affordable:
        return None
    return min(affordable, key=lambda point: (point.missed, -point.threshold_per_mille))


def _rate(count: int, total: int) -> int:
    return 0 if total == 0 else count * FULL_SCALE // total
