from datetime import UTC, datetime, timedelta, timezone

import pytest

from rote.contracts.canonical import canonical_bytes, canonical_hash, utc_iso8601
from rote.contracts.errors import CanonicalisationError


class TestKeyOrderIndependence:
    def test_reordered_keys_produce_identical_bytes(self):
        a = {"alpha": 1, "beta": 2, "gamma": 3}
        b = {"gamma": 3, "alpha": 1, "beta": 2}
        assert canonical_bytes(a) == canonical_bytes(b)

    def test_reordered_nested_keys_produce_identical_bytes(self):
        a = {"outer": {"x": 1, "y": 2}, "z": [{"p": 1, "q": 2}]}
        b = {"z": [{"q": 2, "p": 1}], "outer": {"y": 2, "x": 1}}
        assert canonical_bytes(a) == canonical_bytes(b)

    def test_reordered_keys_produce_identical_hash(self):
        assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


class TestStability:
    def test_output_is_bytes(self):
        assert isinstance(canonical_bytes({"a": 1}), bytes)

    def test_no_insignificant_whitespace(self):
        assert canonical_bytes({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'

    def test_non_ascii_is_preserved_as_utf8(self):
        assert canonical_bytes({"note": "café"}) == '{"note":"café"}'.encode()

    def test_hash_is_sha256_hex(self):
        digest = canonical_hash({"a": 1})
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_repeated_calls_are_identical(self):
        payload = {"b": [1, {"z": None, "a": True}], "a": "x"}
        assert canonical_bytes(payload) == canonical_bytes(payload)


class TestRejectsUnstableTypes:
    def test_float_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"amount": 12.5})

    def test_nested_float_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"outer": [{"rate": 83.25}]})

    def test_non_string_dict_key_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({1: "one"})

    def test_datetime_is_rejected_because_caller_must_normalise_first(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"at": datetime(2026, 8, 22, tzinfo=UTC)})

    def test_arbitrary_object_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"obj": object()})

    def test_set_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"s": {1, 2}})

    def test_bytes_are_rejected(self):
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"b": b"raw"})

    def test_absurdly_deep_nesting_is_rejected_rather_than_crashing(self):
        payload: dict[str, object] = {}
        cursor = payload
        for _ in range(200):
            nested: dict[str, object] = {}
            cursor["next"] = nested
            cursor = nested
        with pytest.raises(CanonicalisationError):
            canonical_bytes(payload)


class TestAcceptsJsonPrimitives:
    def test_accepts_the_full_json_primitive_set(self):
        payload: dict[str, object] = {
            "n": None,
            "t": True,
            "f": False,
            "i": -3,
            "s": "x",
            "l": [],
            "d": {},
        }
        assert (
            canonical_bytes(payload)
            == b'{"d":{},"f":false,"i":-3,"l":[],"n":null,"s":"x","t":true}'
        )

    def test_bool_is_not_treated_as_int(self):
        assert canonical_bytes({"v": True}) != canonical_bytes({"v": 1})

    def test_top_level_scalar_is_allowed(self):
        assert canonical_bytes(5) == b"5"


class TestUtcIso8601:
    def test_utc_datetime_renders_with_z_suffix(self):
        assert utc_iso8601(datetime(2026, 8, 22, 10, 30, 0, tzinfo=UTC)) == "2026-08-22T10:30:00Z"

    def test_microseconds_are_preserved(self):
        moment = datetime(2026, 8, 22, 10, 30, 0, 123456, tzinfo=UTC)
        assert utc_iso8601(moment) == "2026-08-22T10:30:00.123456Z"

    def test_naive_datetime_is_rejected(self):
        with pytest.raises(CanonicalisationError):
            utc_iso8601(datetime(2026, 8, 22, 10, 30, 0))

    def test_non_utc_offset_is_converted_to_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        moment = datetime(2026, 8, 22, 16, 0, 0, tzinfo=ist)
        assert utc_iso8601(moment) == "2026-08-22T10:30:00Z"
