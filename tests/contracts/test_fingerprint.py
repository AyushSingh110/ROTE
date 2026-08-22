import pytest

from rote.contracts.errors import FingerprintError
from rote.contracts.fingerprint import structural_fingerprint, structural_schema


class TestSchemaShape:
    def test_root_scalar(self):
        assert structural_schema(5) == (("$", "int"),)

    def test_flat_object(self):
        assert structural_schema({"a": 1, "b": "x"}) == (
            ("$", "object"),
            ("$.a", "int"),
            ("$.b", "str"),
        )

    def test_nested_object_uses_dotted_paths(self):
        assert structural_schema({"outer": {"inner": None}}) == (
            ("$", "object"),
            ("$.outer", "object"),
            ("$.outer.inner", "null"),
        )

    def test_list_elements_collapse_to_one_path(self):
        assert structural_schema({"xs": [1, 2, 3]}) == (
            ("$", "object"),
            ("$.xs", "list"),
            ("$.xs[]", "int"),
        )

    def test_list_of_objects_describes_element_fields(self):
        assert structural_schema({"rows": [{"id": "a"}, {"id": "b"}]}) == (
            ("$", "object"),
            ("$.rows", "list"),
            ("$.rows[]", "object"),
            ("$.rows[].id", "str"),
        )

    def test_mixed_element_types_produce_both_pairs(self):
        assert structural_schema({"xs": [1, "a"]}) == (
            ("$", "object"),
            ("$.xs", "list"),
            ("$.xs[]", "int"),
            ("$.xs[]", "str"),
        )

    def test_schema_is_sorted_and_deduplicated(self):
        schema = structural_schema({"b": 1, "a": 1, "c": [1, 1, 1]})
        assert list(schema) == sorted(schema)
        assert len(schema) == len(set(schema))


class TestValuesAreExcluded:
    def test_changing_a_value_does_not_change_the_fingerprint(self):
        assert structural_fingerprint({"amount": 100}) == structural_fingerprint({"amount": 999})

    def test_changing_a_string_value_does_not_change_the_fingerprint(self):
        assert structural_fingerprint({"ref": "ORD-4417"}) == structural_fingerprint(
            {"ref": "ORD-5120"}
        )

    def test_list_length_does_not_change_the_fingerprint(self):
        assert structural_fingerprint({"xs": [1]}) == structural_fingerprint({"xs": [1, 2, 3, 4]})


class TestStructureChangesAreDetected:
    def test_key_order_does_not_change_the_fingerprint(self):
        assert structural_fingerprint({"a": 1, "b": 2}) == structural_fingerprint({"b": 2, "a": 1})

    def test_adding_a_key_changes_the_fingerprint(self):
        assert structural_fingerprint({"a": 1}) != structural_fingerprint({"a": 1, "b": 2})

    def test_removing_a_key_changes_the_fingerprint(self):
        assert structural_fingerprint({"a": 1, "b": 2}) != structural_fingerprint({"a": 1})

    def test_changing_a_type_changes_the_fingerprint(self):
        assert structural_fingerprint({"a": 1}) != structural_fingerprint({"a": "1"})

    def test_null_where_an_object_was_expected_changes_the_fingerprint(self):
        assert structural_fingerprint({"a": {"b": 1}}) != structural_fingerprint({"a": None})

    def test_empty_list_differs_from_populated_list(self):
        assert structural_fingerprint({"xs": []}) != structural_fingerprint({"xs": [1]})

    def test_new_element_type_in_a_list_changes_the_fingerprint(self):
        assert structural_fingerprint({"xs": [1]}) != structural_fingerprint({"xs": [1, "a"]})

    def test_bool_is_distinct_from_int(self):
        assert structural_fingerprint({"v": True}) != structural_fingerprint({"v": 1})


class TestDepthCap:
    def test_difference_below_the_depth_cap_is_invisible(self):
        shallow = {"a": {"b": {"c": {"d": 1}}}}
        deeper = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        assert structural_fingerprint(shallow, max_depth=3) == structural_fingerprint(
            deeper, max_depth=3
        )

    def test_truncation_is_marked_in_the_schema(self):
        schema = structural_schema({"a": {"b": {"c": 1}}}, max_depth=2)
        assert ("$.a.b", "truncated") in schema

    def test_difference_above_the_depth_cap_is_visible(self):
        assert structural_fingerprint({"a": {"b": 1}}, max_depth=3) != structural_fingerprint(
            {"a": {"z": 1}}, max_depth=3
        )

    def test_max_depth_must_be_positive(self):
        with pytest.raises(FingerprintError):
            structural_fingerprint({"a": 1}, max_depth=0)


class TestFingerprintFormat:
    def test_fingerprint_is_sha256_hex(self):
        digest = structural_fingerprint({"a": 1})
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_fingerprint_is_stable_across_calls(self):
        payload = {"b": [{"z": 1}], "a": None}
        assert structural_fingerprint(payload) == structural_fingerprint(payload)


class TestRejectsUnstableTypes:
    def test_float_is_rejected(self):
        with pytest.raises(FingerprintError):
            structural_fingerprint({"rate": 83.25})

    def test_non_string_dict_key_is_rejected(self):
        with pytest.raises(FingerprintError):
            structural_fingerprint({1: "one"})

    def test_arbitrary_object_is_rejected(self):
        with pytest.raises(FingerprintError):
            structural_fingerprint({"o": object()})
