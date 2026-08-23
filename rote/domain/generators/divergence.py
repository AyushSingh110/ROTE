from __future__ import annotations

import random
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EXTREME_MULTIPLIER = 10_000
EXTREME_OFFSET = 999_999
UNSEEN_ENUM_VALUE = "seized_by_regulator"
ADDED_FIELD = "unexpected_surcharge"
RETRY_ATTEMPTS = 2


class DivergenceLabel(StrEnum):
    NONE = "none"
    SCHEMA_DRIFT_MISSING = "schema_drift_missing"
    SCHEMA_DRIFT_ADDED = "schema_drift_added"
    TYPE_CHANGE = "type_change"
    EXTREME_VALUE = "extreme_value"
    UNSEEN_ENUM = "unseen_enum"
    RETRIED = "retried"


class DivergenceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: DivergenceLabel
    result: dict[str, Any]
    attempts: int = Field(ge=1)
    # a mutation that could not apply is NOT a divergence: counting one would manufacture a
    # miss that never existed, which is what made the Phase 12 table misleading
    applied: bool


def inject(label: DivergenceLabel, result: dict[str, Any], seed: int) -> DivergenceCase:
    rng = random.Random(seed)
    if label is DivergenceLabel.NONE:
        return DivergenceCase(label=label, result=dict(result), attempts=1, applied=False)
    if label is DivergenceLabel.RETRIED:
        return DivergenceCase(
            label=label, result=dict(result), attempts=RETRY_ATTEMPTS, applied=True
        )
    mutated = _MUTATORS[label](result, rng)
    # a mutation that left the result identical is not a divergence either, however
    # applicable it looked: counting one inflates the miss rate with nothing at all
    changed = mutated is not None and mutated != result
    return DivergenceCase(
        label=label,
        result=mutated if changed else dict(result),
        attempts=1,
        applied=changed,
    )


def divergence_set(result: dict[str, Any], seed: int) -> tuple[DivergenceCase, ...]:
    return tuple(inject(label, result, seed) for label in DivergenceLabel)


def _drop_a_leaf(result: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    holders = _containers_with_leaves(result)
    if not holders:
        return None
    mutated = _clone(result)
    path = holders[rng.randrange(len(holders))]
    container = _walk_to(mutated, path)
    del container[sorted(container)[0]]
    return mutated


def _add_a_field(result: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    del rng
    mutated = _clone(result)
    mutated[ADDED_FIELD] = EXTREME_OFFSET
    return mutated


# only numbers: rendering a string as a string changes nothing, and a no-op mutation
# labelled as a divergence is a fake miss
def _change_a_type(result: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    return _retarget(result, rng, lambda value: str(value), _is_number)


def _explode_a_number(result: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    return _retarget(
        result, rng, lambda value: value * EXTREME_MULTIPLIER + EXTREME_OFFSET, _is_number
    )


def _replace_an_enum(result: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    return _retarget(result, rng, lambda value: UNSEEN_ENUM_VALUE, _is_text)


_MUTATORS = {
    DivergenceLabel.SCHEMA_DRIFT_MISSING: _drop_a_leaf,
    DivergenceLabel.SCHEMA_DRIFT_ADDED: _add_a_field,
    DivergenceLabel.TYPE_CHANGE: _change_a_type,
    DivergenceLabel.EXTREME_VALUE: _explode_a_number,
    DivergenceLabel.UNSEEN_ENUM: _replace_an_enum,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _retarget(
    result: dict[str, Any],
    rng: random.Random,
    change: Any,
    matches: Any,
) -> dict[str, Any] | None:
    targets = _leaf_paths(result, matches)
    if not targets:
        return None
    mutated = _clone(result)
    path = targets[rng.randrange(len(targets))]
    container = _walk_to(mutated, path[:-1])
    container[path[-1]] = change(container[path[-1]])
    return mutated


def _leaf_paths(value: Any, matches: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            found.extend(_leaf_paths(value[key], matches, (*prefix, key)))
    elif matches(value):
        found.append(prefix)
    return found


def _containers_with_leaves(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    if isinstance(value, dict) and value:
        if any(not isinstance(item, dict) for item in value.values()):
            found.append(prefix)
        for key in sorted(value):
            found.extend(_containers_with_leaves(value[key], (*prefix, key)))
    return found


def _walk_to(payload: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    current = payload
    for key in path:
        current = current[key]
    return current


def _clone(value: dict[str, Any]) -> dict[str, Any]:
    return {key: _clone(item) if isinstance(item, dict) else item for key, item in value.items()}
