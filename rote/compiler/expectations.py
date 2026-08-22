from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rote.contracts.fingerprint import structural_fingerprint
from rote.contracts.paths import enumerate_paths
from rote.contracts.plan import StepExpectation

IQR_NUMERATOR = 3
IQR_DENOMINATOR = 2
MAX_CATEGORICAL_VALUES = 20
CATEGORICAL_RATIO_DIVISOR = 4


def learn_expectation(results: Sequence[dict[str, Any]]) -> StepExpectation:
    per_run = [enumerate_paths(result) for result in results]
    shared = set(per_run[0])
    for paths in per_run[1:]:
        shared &= set(paths)

    numeric_observed: dict[str, tuple[int, int]] = {}
    numeric_widened: dict[str, tuple[int, int]] = {}
    categorical: dict[str, frozenset[str]] = {}

    for path in sorted(shared):
        values = [paths[path] for paths in per_run]
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            numeric_observed[path] = (min(values), max(values))
            numeric_widened[path] = _widen(sorted(values))
        elif all(isinstance(v, str) for v in values):
            distinct = frozenset(values)
            if _is_categorical(len(distinct), len(values)):
                categorical[path] = distinct

    return StepExpectation(
        result_fingerprints=frozenset(structural_fingerprint(r) for r in results),
        numeric_observed=numeric_observed,
        numeric_widened=numeric_widened,
        categorical_domains=categorical,
        invariants=(),
        sample_count=len(results),
    )


def _is_categorical(distinct: int, total: int) -> bool:
    return distinct <= MAX_CATEGORICAL_VALUES and distinct * CATEGORICAL_RATIO_DIVISOR <= total


# integer arithmetic throughout: a float tolerance would break canonical comparison
def _widen(ordered: list[int]) -> tuple[int, int]:
    low, high = ordered[0], ordered[-1]
    quarter = len(ordered) // 4
    spread = ordered[-1 - quarter] - ordered[quarter]
    pad = spread * IQR_NUMERATOR // IQR_DENOMINATOR
    floor = max(1, (high - low) // 10)
    return low - pad - floor, high + pad + floor
