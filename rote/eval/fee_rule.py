from __future__ import annotations

from enum import StrEnum

from rote.contracts.common import ExceptionCategory, Money
from rote.contracts.reconciliation import FeeSchedule

BPS_DIVISOR = 10_000


# the pre-registered rule said "internal_amount * percentage_bps / 10_000" without naming a
# rounding. Minor units cannot be fractional, so a choice is forced; FLOOR is the default and
# HALF_UP exists only so the size of that ambiguity can be measured rather than assumed away.
class Rounding(StrEnum):
    FLOOR = "floor"
    HALF_UP = "half_up"


def expected_fee(
    internal_minor_units: int, schedule: FeeSchedule, *, rounding: Rounding = Rounding.FLOOR
) -> int:
    product = internal_minor_units * schedule.percentage_bps
    if rounding is Rounding.HALF_UP:
        percentage = (product + BPS_DIVISOR // 2) // BPS_DIVISOR
    else:
        percentage = product // BPS_DIVISOR
    return schedule.flat_fee_minor_units + percentage


# the rule exactly as pre-registered: the gap is a fee if it equals the fee the schedule
# implies, and otherwise it is not. None means the rule does not apply to this case at all.
def discriminate(
    internal: Money,
    bank: Money | None,
    schedule: FeeSchedule | None,
    *,
    rounding: Rounding = Rounding.FLOOR,
) -> ExceptionCategory | None:
    if bank is None or schedule is None:
        return None
    if bank.currency is not internal.currency or schedule.currency is not internal.currency:
        return None
    shortfall = internal.minor_units - bank.minor_units
    if shortfall <= 0:
        return None
    if shortfall == expected_fee(internal.minor_units, schedule, rounding=rounding):
        return ExceptionCategory.FEE_MISMATCH
    return ExceptionCategory.PARTIAL_PAYMENT


__all__ = ["Rounding", "discriminate", "expected_fee"]
