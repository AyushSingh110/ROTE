from __future__ import annotations

import collections
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from rote.eval.fee_rule import Rounding

FROZEN = ConfigDict(extra="forbid", frozen=True)

# the measured effect of the rounding convention, established by the margin experiment: half-up
# moved every fee case by at most one minor unit. This is a noise floor, not a tolerance.
ROUNDING_BAND = 1
# "comfortably above the band" means an order of magnitude above it
COMFORTABLE_MULTIPLE = 10
# seed-to-seed variation of an order of magnitude counts as unstable
SEED_SPREAD_FACTOR = 10


class Stability(StrEnum):
    STABLE = "stable"
    SHRINKING = "shrinking"
    UNSTABLE = "unstable"


class SweepCell(BaseModel):
    model_config = FROZEN

    size: int = Field(gt=0)
    seed: int
    rounding: Rounding
    fee_count: int = Field(ge=0)
    partial_count: int = Field(ge=0)
    max_fee_distance: int = Field(ge=0)
    min_partial_distance: int = Field(ge=0)
    overlaps: bool
    margin: int | None = None


class TrendPoint(BaseModel):
    model_config = FROZEN

    size: int = Field(gt=0)
    rounding: Rounding
    seeds: int = Field(gt=0)
    worst_min_partial: int = Field(ge=0)
    best_min_partial: int = Field(ge=0)
    worst_max_fee: int = Field(ge=0)
    worst_margin: int | None = None
    any_overlap: bool = False


# the worst seed is the one that counts: an average would hide the dataset that failed
def trend(cells: Sequence[SweepCell]) -> tuple[TrendPoint, ...]:
    grouped: dict[tuple[int, Rounding], list[SweepCell]] = collections.defaultdict(list)
    for cell in cells:
        grouped[(cell.size, cell.rounding)].append(cell)

    points = []
    for size, rounding in sorted(grouped, key=lambda key: (key[0], key[1].value)):
        group = grouped[(size, rounding)]
        worst_partial = min(cell.min_partial_distance for cell in group)
        worst_fee = max(cell.max_fee_distance for cell in group)
        overlap = any(cell.overlaps for cell in group)
        points.append(
            TrendPoint(
                size=size,
                rounding=rounding,
                seeds=len(group),
                worst_min_partial=worst_partial,
                best_min_partial=max(cell.min_partial_distance for cell in group),
                worst_max_fee=worst_fee,
                worst_margin=None if overlap else worst_partial - worst_fee,
                any_overlap=overlap,
            )
        )
    return tuple(points)


# the decision procedure, fixed in writing before any dataset was generated. The order of the
# checks is part of the definition: a failure that has already happened outranks a trend.
def classify(points: Sequence[TrendPoint]) -> Stability:
    if not points:
        raise ValueError("an empty sweep cannot be classified")
    if any(point.any_overlap for point in points):
        return Stability.UNSTABLE
    if any(point.worst_min_partial <= ROUNDING_BAND for point in points):
        return Stability.SHRINKING
    if any(
        point.best_min_partial >= SEED_SPREAD_FACTOR * point.worst_min_partial for point in points
    ):
        return Stability.UNSTABLE
    if _falls_with_sample_size(points):
        return Stability.SHRINKING
    if all(point.worst_min_partial > COMFORTABLE_MULTIPLE * ROUNDING_BAND for point in points):
        return Stability.STABLE
    return Stability.UNSTABLE


def _falls_with_sample_size(points: Sequence[TrendPoint]) -> bool:
    for rounding in Rounding:
        ordered = sorted(
            (point for point in points if point.rounding is rounding), key=lambda p: p.size
        )
        if len(ordered) > 1 and ordered[-1].worst_min_partial < ordered[0].worst_min_partial:
            return True
    return False


__all__ = [
    "COMFORTABLE_MULTIPLE",
    "ROUNDING_BAND",
    "SEED_SPREAD_FACTOR",
    "Stability",
    "SweepCell",
    "TrendPoint",
    "classify",
    "trend",
]
