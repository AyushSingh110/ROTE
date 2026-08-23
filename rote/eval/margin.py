from __future__ import annotations

import statistics
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Money
from rote.contracts.reconciliation import FeeSchedule
from rote.eval.fee_rule import Rounding, expected_fee

FROZEN = ConfigDict(extra="forbid", frozen=True)


class Distribution(BaseModel):
    model_config = FROZEN

    count: int = Field(gt=0)
    minimum: int = Field(ge=0)
    median: int = Field(ge=0)
    maximum: int = Field(ge=0)
    at_zero: int = Field(ge=0)
    within_one: int = Field(ge=0)
    within_two: int = Field(ge=0)


class Separation(BaseModel):
    model_config = FROZEN

    max_fee_distance: int = Field(ge=0)
    min_partial_distance: int = Field(ge=0)
    overlaps: bool
    # the gap between the two ranges; None when they overlap, and never a chosen threshold
    margin: int | None = None


# how far this case's gap sits from the fee the schedule implies. None means the comparison
# does not apply at all, which is reported rather than folded into the numbers.
def distance(
    internal: Money,
    bank: Money | None,
    schedule: FeeSchedule | None,
    *,
    rounding: Rounding = Rounding.FLOOR,
) -> int | None:
    if bank is None or schedule is None:
        return None
    if bank.currency is not internal.currency or schedule.currency is not internal.currency:
        return None
    shortfall = abs(internal.minor_units - bank.minor_units)
    return abs(shortfall - expected_fee(internal.minor_units, schedule, rounding=rounding))


# median_low, so the reported median is a distance some case actually had rather than an
# average of two. Money is integer minor units and this keeps it that way.
def summarise(distances: Sequence[int]) -> Distribution:
    if not distances:
        raise ValueError("an empty set of distances has no statistics to report")
    return Distribution(
        count=len(distances),
        minimum=min(distances),
        median=statistics.median_low(distances),
        maximum=max(distances),
        at_zero=sum(1 for value in distances if value == 0),
        within_one=sum(1 for value in distances if value <= 1),
        within_two=sum(1 for value in distances if value <= 2),
    )


# touching counts as overlapping: a threshold that had to split two equal values would not
# separate anything, so only a strict gap is reported as a margin
def separation(*, fee: Sequence[int], partial: Sequence[int]) -> Separation:
    if not fee or not partial:
        raise ValueError("separation needs both sides; one of them is empty")
    highest, lowest = max(fee), min(partial)
    overlaps = highest >= lowest
    return Separation(
        max_fee_distance=highest,
        min_partial_distance=lowest,
        overlaps=overlaps,
        margin=None if overlaps else lowest - highest,
    )


__all__ = ["Distribution", "Separation", "distance", "separation", "summarise"]
