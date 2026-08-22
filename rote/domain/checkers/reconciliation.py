from __future__ import annotations

from collections.abc import Iterator

from rote.contracts.checker import (
    UNDETERMINED_CODES,
    CheckerMismatch,
    CheckerResult,
    CheckerVerdict,
    MismatchCode,
)
from rote.contracts.errors import CheckerError
from rote.contracts.reconciliation import (
    Adjustment,
    BankStatementLine,
    ExpectedEndState,
    GroundTruth,
    ReconciliationFacts,
    SettlementStatus,
    WorldSnapshot,
)

CHECKER_VERSION = "reconciliation-1"


def check_outcome(
    facts: ReconciliationFacts, ground_truth: GroundTruth, world: WorldSnapshot
) -> CheckerResult:
    if facts.exception_id != ground_truth.exception_id:
        raise CheckerError(
            f"ground truth {ground_truth.exception_id!r} does not describe {facts.exception_id!r}"
        )
    mismatches = tuple(_mismatches(facts, ground_truth.expected_end_state, world))
    return CheckerResult(
        exception_id=facts.exception_id,
        verdict=_verdict(mismatches),
        checker_version=CHECKER_VERSION,
        mismatches=mismatches,
    )


def _verdict(mismatches: tuple[CheckerMismatch, ...]) -> CheckerVerdict:
    if any(mismatch.code in UNDETERMINED_CODES for mismatch in mismatches):
        return CheckerVerdict.UNDETERMINED
    if mismatches:
        return CheckerVerdict.FAIL
    return CheckerVerdict.PASS


def _mismatches(
    facts: ReconciliationFacts, expected: ExpectedEndState, world: WorldSnapshot
) -> Iterator[CheckerMismatch]:
    records = {record.record_id: record for record in world.settlement_records}
    lines = {line.line_id: line for line in world.bank_lines}

    record = records.get(facts.record_id)
    if record is None:
        yield _at(MismatchCode.RECORD_MISSING, f"{facts.record_id} is not in the world")
        return

    for line_id in (expected.matched_bank_line_id, expected.voided_bank_line_id):
        if line_id is not None and line_id not in lines:
            yield _at(MismatchCode.BANK_LINE_MISSING, f"{line_id} is not in the world")

    if record.status is SettlementStatus.UNMATCHED:
        yield _at(MismatchCode.RECORD_NOT_CLOSED, f"{facts.record_id} is still unmatched")
    else:
        if record.status is not expected.settlement_status:
            yield _at(
                MismatchCode.STATUS_MISMATCH,
                f"status is {record.status.value}, expected {expected.settlement_status.value}",
            )
        if record.matched_bank_line_id != expected.matched_bank_line_id:
            yield _at(
                MismatchCode.MATCHED_LINE_MISMATCH,
                f"matched to {record.matched_bank_line_id}, "
                f"expected {expected.matched_bank_line_id}",
            )

    yield from _void_mismatches(facts, expected, lines)
    yield from _adjustment_mismatches(facts, expected, world.adjustments)


def _void_mismatches(
    facts: ReconciliationFacts,
    expected: ExpectedEndState,
    lines: dict[str, BankStatementLine],
) -> Iterator[CheckerMismatch]:
    voided = tuple(
        line_id
        for line_id in facts.candidate_bank_line_ids
        if line_id in lines and lines[line_id].voided
    )
    wanted = () if expected.voided_bank_line_id is None else (expected.voided_bank_line_id,)
    if sorted(voided) != sorted(wanted):
        yield _at(
            MismatchCode.VOIDED_LINE_MISMATCH,
            f"voided {sorted(voided) or 'nothing'}, expected {sorted(wanted) or 'nothing'}",
        )


def _adjustment_mismatches(
    facts: ReconciliationFacts,
    expected: ExpectedEndState,
    adjustments: tuple[Adjustment, ...],
) -> Iterator[CheckerMismatch]:
    posted = [item for item in adjustments if item.record_id == facts.record_id]

    if expected.adjustment_minor_units == 0:
        if posted:
            yield _at(
                MismatchCode.UNEXPECTED_ADJUSTMENT,
                f"{len(posted)} adjustment(s) posted where none was expected",
            )
        return

    if not posted:
        yield _at(
            MismatchCode.ADJUSTMENT_TOTAL_MISMATCH,
            f"expected {expected.adjustment_minor_units}, nothing was posted",
        )
        return

    if len(posted) > 1:
        yield _at(
            MismatchCode.UNEXPECTED_ADJUSTMENT,
            f"{len(posted)} adjustments posted, exactly one was expected",
        )
        return

    only = posted[0]
    if only.amount.minor_units != expected.adjustment_minor_units:
        yield _at(
            MismatchCode.ADJUSTMENT_TOTAL_MISMATCH,
            f"posted {only.amount.minor_units}, expected {expected.adjustment_minor_units}",
        )
    if only.amount.currency is not expected.adjustment_currency:
        yield _at(
            MismatchCode.ADJUSTMENT_CURRENCY_MISMATCH,
            f"posted {only.amount.currency.value}, expected {expected.adjustment_currency}",
        )
    if only.reason is not expected.adjustment_reason:
        yield _at(
            MismatchCode.ADJUSTMENT_REASON_MISMATCH,
            f"posted {only.reason.value}, expected {expected.adjustment_reason}",
        )


def _at(code: MismatchCode, detail: str) -> CheckerMismatch:
    return CheckerMismatch(code=code, detail=detail)
