from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from rote.contracts.errors import RoteError
from rote.contracts.reconciliation import ReconciliationFacts
from rote.domain.tools.adapters import ReconciliationTools

FROZEN = ConfigDict(extra="forbid", frozen=True)
SEARCH_WINDOW_DAYS = 5


class VerificationOutcome(StrEnum):
    AGREEMENT = "agreement"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


class FieldCheck(BaseModel):
    model_config = FROZEN

    field: str
    upstream: str
    authoritative: str
    outcome: VerificationOutcome


class VerificationResult(BaseModel):
    model_config = FROZEN

    exception_id: str
    outcome: VerificationOutcome
    checks: tuple[FieldCheck, ...]
    mismatched_fields: tuple[str, ...]
    unverifiable_fields: tuple[str, ...]


# re-reads the same facts from the world through the existing read tools and compares them with
# the evidence handed in. Exact equality only: no tolerance, no threshold, no model.
def verify(facts: ReconciliationFacts, adapters: ReconciliationTools) -> VerificationResult:
    checks: list[FieldCheck] = []
    record = _read(adapters, "get_settlement_record", {"record_id": facts.record_id})
    if record is None:
        return _result(facts, [_absent("settlement_record", facts.record_id)])

    authoritative = record["record"]
    checks.append(
        _compare(
            "internal_amount",
            facts.internal_amount.minor_units,
            authoritative["amount"]["minor_units"],
        )
    )
    checks.append(
        _compare(
            "currency", facts.internal_amount.currency.value, authoritative["amount"]["currency"]
        )
    )
    checks.append(
        _compare("internal_reference", facts.internal_reference, authoritative["reference"])
    )
    checks.append(
        _compare("captured_on", facts.captured_on.isoformat(), authoritative["captured_on"])
    )
    checks.append(_compare("merchant_id", facts.merchant_id, authoritative["merchant_id"]))

    lines = _authoritative_lines(adapters, authoritative)
    checks.append(_candidate_check(facts, lines))
    checks.extend(_bank_checks(facts, lines))
    return _result(facts, checks)


# the UNION of both authoritative queries, never one as a fallback for the other: a fallback
# that fires only when the primary is empty can silently return a partial set, which is exactly
# the defect the clean control exposed.
def _authoritative_lines(
    adapters: ReconciliationTools, record: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    by_reference = _read(
        adapters, "list_bank_lines_for_reference", {"reference": record["reference"]}
    )
    by_amount = _read(
        adapters,
        "find_bank_lines_by_amount",
        {
            "minor_units": record["amount"]["minor_units"],
            "currency": record["amount"]["currency"],
            "around_date": record["captured_on"],
            "window_days": SEARCH_WINDOW_DAYS,
        },
    )
    ids = sorted(
        set(by_reference["line_ids"] if by_reference else ())
        | set(by_amount["line_ids"] if by_amount else ())
    )
    lines = []
    for line_id in ids:
        payload = _read(adapters, "get_bank_line", {"line_id": line_id})
        if payload is not None:
            lines.append(payload["line"])
    return tuple(lines)


# fixed before running: equal -> agreement; evidence naming a line the world cannot associate
# -> UNVERIFIABLE, never a guess; evidence omitting a line the world confirms -> mismatch.
def _candidate_check(facts: ReconciliationFacts, lines: tuple[dict[str, Any], ...]) -> FieldCheck:
    if not lines:
        return _absent("candidate_bank_line_ids", ", ".join(facts.candidate_bank_line_ids))
    upstream = set(facts.candidate_bank_line_ids)
    found = {str(line["line_id"]) for line in lines}
    if upstream == found:
        outcome = VerificationOutcome.AGREEMENT
    elif upstream - found:
        outcome = VerificationOutcome.UNVERIFIABLE
    else:
        outcome = VerificationOutcome.MISMATCH
    return FieldCheck(
        field="candidate_bank_line_ids",
        upstream=", ".join(sorted(upstream)),
        authoritative=", ".join(sorted(found)),
        outcome=outcome,
    )


# the upstream bank triple must match at least one authoritative line. Matching "any line" rather
# than "the line the upstream points at" keeps the check independent of a corrupted id.
def _bank_checks(facts: ReconciliationFacts, lines: tuple[dict[str, Any], ...]) -> list[FieldCheck]:
    upstream = (
        None if facts.bank_amount is None else facts.bank_amount.minor_units,
        None if facts.bank_amount is None else facts.bank_amount.currency.value,
        None if facts.bank_value_date is None else facts.bank_value_date.isoformat(),
        facts.bank_narration_reference,
    )
    rendered = ("bank_amount", "bank_currency", "bank_value_date", "bank_narration_reference")
    if not lines or facts.bank_amount is None:
        return [_absent(name, str(value)) for name, value in zip(rendered, upstream, strict=True)]

    candidates = [
        (
            line["amount"]["minor_units"],
            line["amount"]["currency"],
            line["value_date"],
            line["narration_reference"],
        )
        for line in lines
    ]
    if any(candidate == upstream for candidate in candidates):
        return [
            FieldCheck(
                field=name,
                upstream=str(value),
                authoritative=str(value),
                outcome=VerificationOutcome.AGREEMENT,
            )
            for name, value in zip(rendered, upstream, strict=True)
        ]

    nearest = candidates[0]
    return [
        _compare(name, value, found)
        for name, value, found in zip(rendered, upstream, nearest, strict=True)
    ]


def _compare(field: str, upstream: object, authoritative: object) -> FieldCheck:
    same = upstream == authoritative
    return FieldCheck(
        field=field,
        upstream=str(upstream),
        authoritative=str(authoritative),
        outcome=VerificationOutcome.AGREEMENT if same else VerificationOutcome.MISMATCH,
    )


def _absent(field: str, upstream: str) -> FieldCheck:
    return FieldCheck(
        field=field,
        upstream=upstream,
        authoritative="<no authoritative source reachable>",
        outcome=VerificationOutcome.UNVERIFIABLE,
    )


# a mismatch outranks an unverifiable field: a contradiction we can see is stronger evidence
# than a field we could not reach, and neither is ever reported as agreement
def _result(facts: ReconciliationFacts, checks: list[FieldCheck]) -> VerificationResult:
    mismatched = tuple(c.field for c in checks if c.outcome is VerificationOutcome.MISMATCH)
    unverifiable = tuple(c.field for c in checks if c.outcome is VerificationOutcome.UNVERIFIABLE)
    if mismatched:
        outcome = VerificationOutcome.MISMATCH
    elif unverifiable:
        outcome = VerificationOutcome.UNVERIFIABLE
    else:
        outcome = VerificationOutcome.AGREEMENT
    return VerificationResult(
        exception_id=facts.exception_id,
        outcome=outcome,
        checks=tuple(checks),
        mismatched_fields=mismatched,
        unverifiable_fields=unverifiable,
    )


def _read(
    adapters: ReconciliationTools, tool: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        return adapters.invoke(tool, payload)
    except RoteError:
        return None


__all__ = ["FieldCheck", "VerificationOutcome", "VerificationResult", "verify"]
