from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rote.contracts.errors import GuardError

Invariant = Callable[[dict[str, Any], dict[str, Any]], bool]


def _adjustment_within_internal_amount(
    arguments: dict[str, Any], task_input: dict[str, Any]
) -> bool:
    posted = _as_int(arguments.get("minor_units"))
    ceiling = _as_int(_dig(task_input, "internal_amount", "minor_units"))
    if posted is None or ceiling is None:
        return False
    return abs(posted) <= abs(ceiling)


def _adjustment_currency_matches_the_bank_line(
    arguments: dict[str, Any], task_input: dict[str, Any]
) -> bool:
    posted = arguments.get("currency")
    expected = _dig(task_input, "bank_amount", "currency")
    if not isinstance(posted, str) or not isinstance(expected, str):
        return False
    return posted == expected


def _settles_against_a_candidate_line(
    arguments: dict[str, Any], task_input: dict[str, Any]
) -> bool:
    return _is_candidate(arguments.get("bank_line_id"), task_input)


def _voids_only_a_candidate_line(arguments: dict[str, Any], task_input: dict[str, Any]) -> bool:
    return _is_candidate(arguments.get("line_id"), task_input)


# closed and hand-written: a plan names an invariant, it can never carry one
INVARIANTS: dict[str, Invariant] = {
    "adjustment_within_internal_amount": _adjustment_within_internal_amount,
    "adjustment_currency_matches_the_bank_line": _adjustment_currency_matches_the_bank_line,
    "settles_against_a_candidate_line": _settles_against_a_candidate_line,
    "voids_only_a_candidate_line": _voids_only_a_candidate_line,
}


def evaluate_invariants(
    names: Sequence[str], arguments: dict[str, Any], task_input: dict[str, Any]
) -> tuple[str, ...]:
    failed: list[str] = []
    for name in names:
        invariant = INVARIANTS.get(name)
        if invariant is None:
            raise GuardError(f"no invariant named {name!r}")
        if not invariant(arguments, task_input):
            failed.append(name)
    return tuple(failed)


def _is_candidate(value: object, task_input: dict[str, Any]) -> bool:
    candidates = task_input.get("candidate_bank_line_ids")
    if not isinstance(value, str) or not isinstance(candidates, list | tuple):
        return False
    return value in candidates


def _dig(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


# a missing field fails the check rather than passing it: absence is not evidence of safety
def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
