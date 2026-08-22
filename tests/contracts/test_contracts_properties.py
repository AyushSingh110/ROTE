import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from rote.contracts.canonical import canonical_bytes
from rote.contracts.fingerprint import structural_fingerprint

json_scalars = st.none() | st.booleans() | st.integers() | st.text()
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(min_size=1), children, max_size=4),
    max_leaves=12,
)


def reorder_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(reversed([(k, reorder_keys(v)) for k, v in value.items()]))
    if isinstance(value, list):
        return [reorder_keys(v) for v in value]
    return value


def blank_scalars(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: blank_scalars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [blank_scalars(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return 0
    if isinstance(value, str):
        return ""
    return value


@given(json_values)
def test_canonical_bytes_ignore_key_order(value):
    assert canonical_bytes(value) == canonical_bytes(reorder_keys(value))


@given(json_values)
def test_canonical_bytes_round_trip_to_an_equal_structure(value):
    assert json.loads(canonical_bytes(value).decode()) == value


@given(json_values)
def test_canonical_bytes_are_deterministic(value):
    assert canonical_bytes(value) == canonical_bytes(value)


@given(json_values)
def test_fingerprint_ignores_key_order(value):
    assert structural_fingerprint(value) == structural_fingerprint(reorder_keys(value))


@given(json_values)
def test_fingerprint_ignores_scalar_values(value):
    assert structural_fingerprint(value) == structural_fingerprint(blank_scalars(value))


@given(st.dictionaries(st.text(min_size=1), json_scalars, max_size=4))
def test_adding_a_key_changes_the_fingerprint(mapping):
    fresh_key = "".join(mapping.keys()) + "_rote_fresh_key"
    extended = {**mapping, fresh_key: 1}
    assert structural_fingerprint(mapping) != structural_fingerprint(extended)
