from __future__ import annotations

from typing import Any

from rote.contracts.common import ExceptionCategory

# deterministic predicates over STRUCTURED fields only. They exist so the router can disagree
# with the classifier: a component cannot cross-check itself, and a nudged label is the
# injection that actually works (ARCHITECTURE.md SS F/T2). They are necessary, not sufficient.


def precondition_holds(category: ExceptionCategory, facts: dict[str, Any]) -> bool:
    check = _PRECONDITIONS.get(category)
    return False if check is None else check(facts)


def _same_currency_shortfall(facts: dict[str, Any]) -> bool:
    internal, bank = _amounts(facts)
    if internal is None or bank is None:
        return False
    return internal[1] == bank[1] and bank[0] < internal[0]


def _cross_currency(facts: dict[str, Any]) -> bool:
    internal, bank = _amounts(facts)
    if internal is None or bank is None:
        return False
    return internal[1] != bank[1]


def _settled_late_in_full(facts: dict[str, Any]) -> bool:
    internal, bank = _amounts(facts)
    if internal is None or bank is None or internal != bank:
        return False
    captured, value_date = facts.get("captured_on"), facts.get("bank_value_date")
    if not isinstance(captured, str) or not isinstance(value_date, str):
        return False
    return value_date > captured


def _reference_is_a_rearrangement(facts: dict[str, Any]) -> bool:
    internal, bank = _amounts(facts)
    if internal is None or bank is None or internal != bank:
        return False
    ours, theirs = facts.get("internal_reference"), facts.get("bank_narration_reference")
    if not isinstance(ours, str) or not isinstance(theirs, str) or ours == theirs:
        return False
    return sorted(ours) == sorted(theirs)


def _two_lines_for_one_reference(facts: dict[str, Any]) -> bool:
    candidates = facts.get("candidate_bank_line_ids")
    if not isinstance(candidates, list | tuple) or len(candidates) < 2:
        return False
    ours, theirs = facts.get("internal_reference"), facts.get("bank_narration_reference")
    return isinstance(ours, str) and ours == theirs


_PRECONDITIONS = {
    ExceptionCategory.TIMING_CUTOFF: _settled_late_in_full,
    ExceptionCategory.FEE_MISMATCH: _same_currency_shortfall,
    ExceptionCategory.PARTIAL_PAYMENT: _same_currency_shortfall,
    ExceptionCategory.FX_ROUNDING: _cross_currency,
    ExceptionCategory.TRANSPOSED_REFERENCE: _reference_is_a_rearrangement,
    ExceptionCategory.DUPLICATE_ENTRY: _two_lines_for_one_reference,
}


def _amounts(facts: dict[str, Any]) -> tuple[tuple[int, str] | None, tuple[int, str] | None]:
    return _money(facts.get("internal_amount")), _money(facts.get("bank_amount"))


def _money(value: Any) -> tuple[int, str] | None:
    if not isinstance(value, dict):
        return None
    minor, currency = value.get("minor_units"), value.get("currency")
    if isinstance(minor, bool) or not isinstance(minor, int) or not isinstance(currency, str):
        return None
    return minor, currency
