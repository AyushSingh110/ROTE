from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum

from rote.contracts.common import Currency, ExceptionCategory, Money
from rote.contracts.reconciliation import (
    GeneratedDataset,
    ReconciliationException,
    ReconciliationFacts,
)

PLAUSIBLE_SHIFT_MINOR_UNITS = 500
TIMESTAMP_SHIFT_DAYS = 3
SUBSTITUTE_REFERENCE = "REF99999999"
SUBSTITUTE_MERCHANT = "MRC-substitute"
SUBSTITUTE_LINE = "BNK-000002"


# the corruption classes, fixed in writing before any case was run. Each one models an upstream
# extraction or data error: the underlying world is untouched, only the evidence handed to Rote.
class EvidenceError(StrEnum):
    NONE = "none"
    AMOUNT_OFF_BY_ONE = "amount_off_by_one"
    AMOUNT_PLAUSIBLE_SHIFT = "amount_plausible_shift"
    CURRENCY_SUBSTITUTION = "currency_substitution"
    REFERENCE_SUBSTITUTION = "reference_substitution"
    TIMESTAMP_SHIFT = "timestamp_shift"
    MISSING_FIELD = "missing_field"
    UNREAD_FIELD = "unread_field"
    CANDIDATE_SUBSTITUTION = "candidate_substitution"
    CROSS_CATEGORY = "cross_category"


def corrupt(
    facts: ReconciliationFacts,
    error: EvidenceError,
    truth_of: Mapping[str, ExceptionCategory],
) -> tuple[ReconciliationFacts, bool]:
    if error is EvidenceError.NONE:
        return facts, False
    if error is EvidenceError.CROSS_CATEGORY:
        truth = truth_of.get(facts.exception_id)
        return _cross_category(facts, truth), True
    if error is EvidenceError.UNREAD_FIELD:
        return facts.model_copy(update={"merchant_id": SUBSTITUTE_MERCHANT}), True
    if error is EvidenceError.CANDIDATE_SUBSTITUTION:
        swapped = (SUBSTITUTE_LINE, *facts.candidate_bank_line_ids[1:])
        if swapped == facts.candidate_bank_line_ids:
            return facts, False
        return facts.model_copy(update={"candidate_bank_line_ids": swapped}), True
    if error is EvidenceError.MISSING_FIELD:
        return facts.model_copy(update={"bank_amount": None}), True
    if error is EvidenceError.REFERENCE_SUBSTITUTION:
        return facts.model_copy(update={"bank_narration_reference": SUBSTITUTE_REFERENCE}), True
    if error is EvidenceError.TIMESTAMP_SHIFT:
        base = facts.bank_value_date or facts.captured_on
        shifted = base + timedelta(days=TIMESTAMP_SHIFT_DAYS)
        return facts.model_copy(update={"bank_value_date": shifted}), True

    bank = facts.bank_amount
    if bank is None:
        # nothing to move; reported as not applied rather than silently counted as a corruption
        return facts, False
    if error is EvidenceError.AMOUNT_OFF_BY_ONE:
        moved = Money(minor_units=bank.minor_units + 1, currency=bank.currency)
    elif error is EvidenceError.AMOUNT_PLAUSIBLE_SHIFT:
        lowered = max(1, bank.minor_units - PLAUSIBLE_SHIFT_MINOR_UNITS)
        moved = Money(minor_units=lowered, currency=bank.currency)
    else:
        moved = Money(minor_units=bank.minor_units, currency=_other_currency(bank.currency))
    return facts.model_copy(update={"bank_amount": moved}), True


# rewrites the evidence so exactly one WRONG category's precondition fits, while every value
# stays schema-valid and internally consistent
def _cross_category(
    facts: ReconciliationFacts, truth: ExceptionCategory | None
) -> ReconciliationFacts:
    if truth is ExceptionCategory.TIMING_CUTOFF:
        return _looks_like_fx_rounding(facts)
    return _looks_like_timing_cutoff(facts)


# amounts equal, bank posted later, references identical, one candidate line: timing_cutoff is
# the only category whose precondition holds
def _looks_like_timing_cutoff(facts: ReconciliationFacts) -> ReconciliationFacts:
    return facts.model_copy(
        update={
            "bank_amount": Money(
                minor_units=facts.internal_amount.minor_units,
                currency=facts.internal_amount.currency,
            ),
            "bank_value_date": facts.captured_on + timedelta(days=TIMESTAMP_SHIFT_DAYS),
            "bank_narration_reference": facts.internal_reference,
            "candidate_bank_line_ids": facts.candidate_bank_line_ids[:1],
        }
    )


# a different currency on the bank side: fx_rounding is the only category that fits
def _looks_like_fx_rounding(facts: ReconciliationFacts) -> ReconciliationFacts:
    bank = facts.bank_amount or facts.internal_amount
    return facts.model_copy(
        update={
            "bank_amount": Money(
                minor_units=bank.minor_units,
                currency=_other_currency(facts.internal_amount.currency),
            ),
            "bank_value_date": facts.captured_on,
            "bank_narration_reference": facts.internal_reference,
            "candidate_bank_line_ids": facts.candidate_bank_line_ids[:1],
        }
    )


CURRENCY_ORDER: tuple[Currency, ...] = (Currency.EUR, Currency.INR, Currency.USD)


def _other_currency(currency: Currency) -> Currency:
    return CURRENCY_ORDER[(CURRENCY_ORDER.index(currency) + 1) % len(CURRENCY_ORDER)]


# the world and the ground truth are carried across untouched: only the evidence Rote reads moves
def corrupted_dataset(
    data: GeneratedDataset,
    error: EvidenceError,
    truth_of: Mapping[str, ExceptionCategory],
) -> GeneratedDataset:
    exceptions = tuple(
        ReconciliationException(
            exception_id=exception.exception_id,
            domain=exception.domain,
            facts=corrupt(exception.facts, error, truth_of)[0],
            untrusted=exception.untrusted,
        )
        for exception in data.exceptions
    )
    return data.model_copy(update={"exceptions": exceptions})


__all__ = ["EvidenceError", "corrupt", "corrupted_dataset"]
