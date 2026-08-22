from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

FROZEN = ConfigDict(extra="forbid", frozen=True)


class CheckerVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNDETERMINED = "undetermined"


class MismatchCode(StrEnum):
    RECORD_MISSING = "record_missing"
    BANK_LINE_MISSING = "bank_line_missing"
    RECORD_NOT_CLOSED = "record_not_closed"
    STATUS_MISMATCH = "status_mismatch"
    MATCHED_LINE_MISMATCH = "matched_line_mismatch"
    VOIDED_LINE_MISMATCH = "voided_line_mismatch"
    ADJUSTMENT_TOTAL_MISMATCH = "adjustment_total_mismatch"
    ADJUSTMENT_CURRENCY_MISMATCH = "adjustment_currency_mismatch"
    ADJUSTMENT_REASON_MISMATCH = "adjustment_reason_mismatch"
    UNEXPECTED_ADJUSTMENT = "unexpected_adjustment"


# these mean the run cannot be judged, not that it was wrong; they never count as a failure
UNDETERMINED_CODES: frozenset[MismatchCode] = frozenset(
    {
        MismatchCode.RECORD_MISSING,
        MismatchCode.BANK_LINE_MISSING,
        MismatchCode.RECORD_NOT_CLOSED,
    }
)


class CheckerMismatch(BaseModel):
    model_config = FROZEN

    code: MismatchCode
    detail: str


class CheckerResult(BaseModel):
    model_config = FROZEN

    exception_id: str = Field(min_length=1)
    verdict: CheckerVerdict
    checker_version: str = Field(min_length=1)
    mismatches: tuple[CheckerMismatch, ...]
