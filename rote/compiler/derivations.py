from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NamedTuple

from rote.contracts.errors import CompilerError

MICROS = 1_000_000


class Derivation(NamedTuple):
    arity: int
    apply: Callable[..., int]


def _difference(first: int, second: int) -> int:
    return first - second


def _sum(first: int, second: int) -> int:
    return first + second


def _scaled_difference(first: int, second: int, scale_micros: int) -> int:
    return (first * scale_micros // MICROS) - second


def _scaled_sum(first: int, second: int, scale_micros: int) -> int:
    return (first * scale_micros // MICROS) + second


# a closed, hand-written registry: adding a formula is a code change with a test, never data
DERIVATIONS: dict[str, Derivation] = {
    "difference": Derivation(2, _difference),
    "sum": Derivation(2, _sum),
    "scaled_difference": Derivation(3, _scaled_difference),
    "scaled_sum": Derivation(3, _scaled_sum),
}

# simplest first, so the binder prefers the fewest operands and ties break the same way each run
SEARCH_ORDER: tuple[str, ...] = tuple(
    sorted(DERIVATIONS, key=lambda name: (DERIVATIONS[name].arity, name))
)


def apply_derivation(derivation_id: str, operands: Sequence[int]) -> int:
    derivation = DERIVATIONS.get(derivation_id)
    if derivation is None:
        raise CompilerError(f"no formula named {derivation_id!r}")
    if len(operands) != derivation.arity:
        raise CompilerError(
            f"formula {derivation_id!r} takes {derivation.arity} operands, got {len(operands)}"
        )
    return derivation.apply(*operands)
