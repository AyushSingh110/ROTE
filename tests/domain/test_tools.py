import ast
import pathlib
from typing import Any

import pytest

from rote.contracts.canonical import canonical_bytes
from rote.contracts.errors import RecordNotFoundError, ToolRequestError, UnknownToolError
from rote.contracts.fingerprint import structural_fingerprint
from rote.contracts.reconciliation import AdjustmentReason, SettlementStatus
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.domain.tools.registry import MUTATING_TOOLS, TOOL_NAMES

SEED = 11
COUNT = 60

DOMAIN_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "domain"

BANNED_IMPORT_ROOTS = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "openai",
        "anthropic",
        "groq",
        "ollama",
        "langchain",
        "langgraph",
        "sklearn",
        "torch",
        "transformers",
        "sentence_transformers",
        "sqlalchemy",
    }
)


def build_tools() -> ReconciliationTools:
    dataset = generate_dataset(seed=SEED, count=COUNT)
    return ReconciliationTools.from_snapshot(dataset.world)


def sample_payloads() -> dict[str, dict[str, Any]]:
    dataset = generate_dataset(seed=SEED, count=COUNT)
    record = dataset.world.settlement_records[0]
    line = dataset.world.bank_lines[0]
    rate = dataset.world.fx_rates[0]
    return {
        "get_settlement_record": {"record_id": record.record_id},
        "get_bank_line": {"line_id": line.line_id},
        "find_bank_lines_by_amount": {
            "minor_units": line.amount.minor_units,
            "currency": line.amount.currency.value,
            "around_date": line.value_date.isoformat(),
            "window_days": 5,
        },
        "list_bank_lines_for_reference": {"reference": line.narration_reference},
        "get_fee_schedule": {"merchant_id": record.merchant_id},
        "get_fx_rate": {
            "base": rate.base.value,
            "quote": rate.quote.value,
            "rate_date": rate.rate_date.isoformat(),
        },
        "get_merchant_profile": {"merchant_id": record.merchant_id},
        "get_chargeback_history": {"order_id": record.order_id},
        "recalculate_settlement_batch": {"batch_id": line.batch_id},
        "post_adjustment": {
            "record_id": record.record_id,
            "minor_units": -250,
            "currency": record.amount.currency.value,
            "reason": AdjustmentReason.FEE.value,
            "idempotency_key": "idem-post-1",
        },
        "mark_settlement_matched": {
            "record_id": record.record_id,
            "bank_line_id": line.line_id,
            "status": SettlementStatus.MATCHED.value,
            "idempotency_key": "idem-match-1",
        },
        "void_duplicate_bank_line": {
            "line_id": line.line_id,
            "idempotency_key": "idem-void-1",
        },
    }


class TestRegistry:
    def test_every_named_tool_has_a_sample_payload_in_this_test(self):
        assert set(TOOL_NAMES) == set(sample_payloads())

    def test_tool_names_are_sorted_for_deterministic_iteration(self):
        assert list(TOOL_NAMES) == sorted(TOOL_NAMES)

    def test_the_tool_set_is_a_superset_of_what_any_category_needs(self):
        assert len(TOOL_NAMES) >= 12

    def test_plausible_but_unnecessary_decoy_tools_are_present(self):
        assert {"get_chargeback_history", "get_merchant_profile"} <= set(TOOL_NAMES)

    def test_mutating_tools_are_declared_explicitly(self):
        assert set(MUTATING_TOOLS) == {
            "post_adjustment",
            "mark_settlement_matched",
            "void_duplicate_bank_line",
        }


class TestDeterminism:
    def test_repeated_identical_calls_return_identical_bytes(self):
        tools = build_tools()
        for name, payload in sample_payloads().items():
            if name in MUTATING_TOOLS:
                continue
            first = canonical_bytes(tools.invoke(name, payload))
            for _ in range(5):
                assert canonical_bytes(tools.invoke(name, payload)) == first

    def test_a_rebuilt_world_from_the_same_seed_answers_identically(self):
        one, other = build_tools(), build_tools()
        for name, payload in sample_payloads().items():
            if name in MUTATING_TOOLS:
                continue
            assert canonical_bytes(one.invoke(name, payload)) == canonical_bytes(
                other.invoke(name, payload)
            )

    def test_result_fingerprints_are_stable_across_worlds(self):
        one, other = build_tools(), build_tools()
        for name, payload in sample_payloads().items():
            if name in MUTATING_TOOLS:
                continue
            assert structural_fingerprint(one.invoke(name, payload)) == structural_fingerprint(
                other.invoke(name, payload)
            )

    def test_every_tool_returns_a_canonically_serialisable_result(self):
        tools = build_tools()
        for name, payload in sample_payloads().items():
            assert canonical_bytes(tools.invoke(name, payload))


class TestReadsDoNotMutate:
    def test_read_only_tools_leave_the_world_unchanged(self):
        tools = build_tools()
        before = canonical_bytes(tools.snapshot().model_dump(mode="json"))
        for name, payload in sample_payloads().items():
            if name in MUTATING_TOOLS:
                continue
            tools.invoke(name, payload)
        assert canonical_bytes(tools.snapshot().model_dump(mode="json")) == before

    def test_a_mutating_tool_does_change_the_world(self):
        tools = build_tools()
        before = canonical_bytes(tools.snapshot().model_dump(mode="json"))
        tools.invoke("post_adjustment", sample_payloads()["post_adjustment"])
        assert canonical_bytes(tools.snapshot().model_dump(mode="json")) != before


class TestIdempotency:
    def test_replaying_the_same_key_posts_one_adjustment(self):
        tools = build_tools()
        payload = sample_payloads()["post_adjustment"]
        first = tools.invoke("post_adjustment", payload)
        second = tools.invoke("post_adjustment", payload)
        assert first == second
        assert len(tools.snapshot().adjustments) == 1

    def test_a_new_key_posts_a_second_adjustment(self):
        tools = build_tools()
        payload = sample_payloads()["post_adjustment"]
        tools.invoke("post_adjustment", payload)
        tools.invoke("post_adjustment", {**payload, "idempotency_key": "idem-post-2"})
        assert len(tools.snapshot().adjustments) == 2

    def test_replaying_a_match_leaves_one_matched_record(self):
        tools = build_tools()
        payload = sample_payloads()["mark_settlement_matched"]
        first = tools.invoke("mark_settlement_matched", payload)
        second = tools.invoke("mark_settlement_matched", payload)
        assert first == second
        matched = [
            record
            for record in tools.snapshot().settlement_records
            if record.status is SettlementStatus.MATCHED
        ]
        assert len(matched) == 1

    def test_replaying_a_void_leaves_one_voided_line(self):
        tools = build_tools()
        payload = sample_payloads()["void_duplicate_bank_line"]
        tools.invoke("void_duplicate_bank_line", payload)
        tools.invoke("void_duplicate_bank_line", payload)
        voided = [line for line in tools.snapshot().bank_lines if line.voided]
        assert len(voided) == 1

    def test_reusing_a_key_for_a_different_action_is_rejected(self):
        tools = build_tools()
        payload = sample_payloads()["post_adjustment"]
        tools.invoke("post_adjustment", payload)
        with pytest.raises(ToolRequestError):
            tools.invoke("post_adjustment", {**payload, "minor_units": -999})


class TestBoundaryValidation:
    def test_an_unknown_tool_name_is_rejected(self):
        with pytest.raises(UnknownToolError):
            build_tools().invoke("definitely_not_a_tool", {})

    def test_missing_arguments_are_rejected(self):
        with pytest.raises(ToolRequestError):
            build_tools().invoke("get_settlement_record", {})

    def test_unknown_arguments_are_rejected(self):
        with pytest.raises(ToolRequestError):
            build_tools().invoke(
                "get_settlement_record", {"record_id": "REC-000000", "surprise": 1}
            )

    def test_wrongly_typed_arguments_are_rejected(self):
        with pytest.raises(ToolRequestError):
            build_tools().invoke(
                "find_bank_lines_by_amount",
                {
                    "minor_units": "not-a-number",
                    "currency": "INR",
                    "around_date": "2026-06-01",
                    "window_days": 5,
                },
            )

    def test_a_float_argument_is_rejected_because_money_is_minor_units(self):
        with pytest.raises(ToolRequestError):
            build_tools().invoke(
                "post_adjustment",
                {
                    "record_id": "REC-000000",
                    "minor_units": 12.5,
                    "currency": "INR",
                    "reason": AdjustmentReason.FEE.value,
                    "idempotency_key": "idem-float",
                },
            )

    def test_an_unknown_record_raises_rather_than_returning_nothing(self):
        with pytest.raises(RecordNotFoundError):
            build_tools().invoke("get_settlement_record", {"record_id": "REC-999999"})

    def test_an_unknown_bank_line_raises(self):
        with pytest.raises(RecordNotFoundError):
            build_tools().invoke("get_bank_line", {"line_id": "BNK-999999"})

    def test_an_unknown_fee_schedule_raises(self):
        with pytest.raises(RecordNotFoundError):
            build_tools().invoke("get_fee_schedule", {"merchant_id": "MER-999"})

    def test_a_search_with_no_hits_returns_an_empty_list_not_an_error(self):
        result = build_tools().invoke(
            "find_bank_lines_by_amount",
            {
                "minor_units": 999_999_999,
                "currency": "INR",
                "around_date": "2026-06-01",
                "window_days": 1,
            },
        )
        assert result["line_ids"] == []


class TestOfflineAndFreeOfHigherLayers:
    def test_no_domain_module_imports_a_network_or_model_library(self):
        for path in sorted(DOMAIN_PACKAGE.rglob("*.py")):
            for root in _imported_roots(path):
                assert root not in BANNED_IMPORT_ROOTS, f"{path.name} imports {root}"

    def test_no_domain_module_imports_agent_compiler_or_runtime(self):
        forbidden = {"rote.agent", "rote.compiler", "rote.runtime", "rote.eval", "rote.service"}
        for path in sorted(DOMAIN_PACKAGE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for module in forbidden:
                assert module not in source, f"{path.name} references {module}"


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots
