import ast
import inspect
import pathlib

import pytest
from pydantic import ValidationError

from rote.contracts.canonical import canonical_bytes
from rote.contracts.checker import (
    UNDETERMINED_CODES,
    CheckerResult,
    CheckerVerdict,
    MismatchCode,
)
from rote.contracts.errors import CheckerError
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationFacts
from rote.domain.checkers.reconciliation import CHECKER_VERSION, check_outcome
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from tests.domain.reference_resolver import resolve

SEED = 13
COUNT = 60
BULK_COUNT = 500

CHECKER_MODULE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "domain" / "checkers"


def dataset(count: int = COUNT) -> GeneratedDataset:
    return generate_dataset(seed=SEED, count=count)


def resolved_tools(
    data: GeneratedDataset,
    *,
    close_first: bool = False,
    corruption: str | None = None,
) -> ReconciliationTools:
    tools = ReconciliationTools.from_snapshot(data.world)
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    last_line = data.world.bank_lines[-1].line_id
    first_line = data.world.bank_lines[0].line_id
    for exception in data.exceptions:
        truth = truths[exception.exception_id]
        expected = truth.expected_end_state.matched_bank_line_id
        fallback = last_line if expected != last_line else first_line
        resolve(
            tools,
            exception,
            truth,
            close_first=close_first,
            corruption=corruption,
            fallback_line_id=fallback,
        )
    return tools


def check_all(data: GeneratedDataset, tools: ReconciliationTools) -> list[CheckerResult]:
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    world = tools.snapshot()
    return [
        check_outcome(exception.facts, truths[exception.exception_id], world)
        for exception in data.exceptions
    ]


def check_one(data: GeneratedDataset, tools: ReconciliationTools, index: int) -> CheckerResult:
    exception = data.exceptions[index]
    truth = next(t for t in data.ground_truths if t.exception_id == exception.exception_id)
    return check_outcome(exception.facts, truth, tools.snapshot())


def index_of(data: GeneratedDataset, predicate: object) -> int:
    for position, truth in enumerate(data.ground_truths):
        if predicate(truth.expected_end_state):  # type: ignore[operator]
            return position
    raise AssertionError("no exception matched the predicate")


class TestVerdictContract:
    def test_exactly_three_verdicts_exist(self):
        assert {verdict.value for verdict in CheckerVerdict} == {
            "pass",
            "fail",
            "undetermined",
        }

    def test_undetermined_codes_are_a_subset_of_all_codes(self):
        assert UNDETERMINED_CODES.issubset(MismatchCode)

    def test_result_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            CheckerResult(
                exception_id="EXC-000000",
                verdict=CheckerVerdict.PASS,
                checker_version=CHECKER_VERSION,
                mismatches=(),
                surprise=1,
            )

    def test_checker_version_is_recorded_on_every_result(self):
        data = dataset()
        results = check_all(data, ReconciliationTools.from_snapshot(data.world))
        assert all(result.checker_version == CHECKER_VERSION for result in results)


class TestCheckerSeesOnlyTheEndState:
    def test_the_signature_exposes_no_execution_detail(self):
        rendered = str(inspect.signature(check_outcome)).lower()
        for banned in ("trajectory", "step", "tool", "confidence", "reasoning", "plan"):
            assert banned not in rendered

    def test_the_signature_takes_facts_ground_truth_and_world_only(self):
        assert list(inspect.signature(check_outcome).parameters) == [
            "facts",
            "ground_truth",
            "world",
        ]

    def test_the_checker_cannot_be_handed_untrusted_text(self):
        assert "untrusted" not in str(inspect.signature(check_outcome)).lower()
        assert "untrusted" not in set(ReconciliationFacts.model_fields)

    def test_the_checker_imports_no_model_or_higher_layer(self):
        banned = {
            "openai",
            "anthropic",
            "groq",
            "ollama",
            "langchain",
            "langgraph",
            "sklearn",
            "torch",
            "random",
            "httpx",
            "requests",
        }
        for path in sorted(CHECKER_MODULE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not {a.name.split(".")[0] for a in node.names} & banned
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in banned

    def test_the_reference_oracle_is_never_imported_by_production_code(self):
        package = pathlib.Path(__file__).resolve().parents[2] / "rote"
        for path in sorted(package.rglob("*.py")):
            assert "reference_resolver" not in path.read_text(encoding="utf-8")

    def test_a_ground_truth_for_a_different_exception_is_rejected(self):
        data = dataset()
        with pytest.raises(CheckerError):
            check_outcome(data.exceptions[0].facts, data.ground_truths[1], data.world)


class TestCorrectResolutionPasses:
    def test_a_correctly_resolved_world_passes_every_exception(self):
        data = dataset()
        results = check_all(data, resolved_tools(data))
        assert {result.verdict for result in results} == {CheckerVerdict.PASS}

    def test_a_passing_result_lists_no_mismatches(self):
        data = dataset()
        assert all(result.mismatches == () for result in check_all(data, resolved_tools(data)))

    def test_the_verdict_does_not_depend_on_the_order_of_the_steps(self):
        data = dataset()
        forward = check_all(data, resolved_tools(data))
        reversed_order = check_all(data, resolved_tools(data, close_first=True))
        assert [r.verdict for r in forward] == [r.verdict for r in reversed_order]
        assert {r.verdict for r in reversed_order} == {CheckerVerdict.PASS}


class TestUnfinishedWorkIsUndetermined:
    def test_an_untouched_world_is_undetermined_everywhere(self):
        data = dataset()
        results = check_all(data, ReconciliationTools.from_snapshot(data.world))
        assert {result.verdict for result in results} == {CheckerVerdict.UNDETERMINED}

    def test_an_untouched_world_reports_the_record_as_not_closed(self):
        data = dataset()
        results = check_all(data, ReconciliationTools.from_snapshot(data.world))
        assert all(
            MismatchCode.RECORD_NOT_CLOSED in {m.code for m in result.mismatches}
            for result in results
        )

    def test_skipping_only_the_close_is_undetermined(self):
        data = dataset()
        result = check_one(data, resolved_tools(data, corruption="skip_close"), 0)
        assert result.verdict is CheckerVerdict.UNDETERMINED

    def test_a_missing_record_is_undetermined_not_a_failure(self):
        data = dataset()
        tools = resolved_tools(data)
        world = tools.snapshot()
        stripped = world.model_copy(
            update={
                "settlement_records": tuple(
                    record
                    for record in world.settlement_records
                    if record.record_id != data.exceptions[0].facts.record_id
                )
            }
        )
        result = check_outcome(data.exceptions[0].facts, data.ground_truths[0], stripped)
        assert result.verdict is CheckerVerdict.UNDETERMINED
        assert MismatchCode.RECORD_MISSING in {m.code for m in result.mismatches}

    def test_a_missing_expected_bank_line_is_undetermined(self):
        data = dataset()
        tools = resolved_tools(data)
        world = tools.snapshot()
        expected_line = data.ground_truths[0].expected_end_state.matched_bank_line_id
        stripped = world.model_copy(
            update={
                "bank_lines": tuple(
                    line for line in world.bank_lines if line.line_id != expected_line
                )
            }
        )
        result = check_outcome(data.exceptions[0].facts, data.ground_truths[0], stripped)
        assert result.verdict is CheckerVerdict.UNDETERMINED
        assert MismatchCode.BANK_LINE_MISSING in {m.code for m in result.mismatches}


class TestWrongResolutionFails:
    def test_matching_the_wrong_bank_line_fails(self):
        data = dataset()
        result = check_one(data, resolved_tools(data, corruption="wrong_line"), 0)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.MATCHED_LINE_MISMATCH in {m.code for m in result.mismatches}

    def test_closing_with_the_wrong_status_fails(self):
        data = dataset()
        result = check_one(data, resolved_tools(data, corruption="wrong_status"), 0)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.STATUS_MISMATCH in {m.code for m in result.mismatches}

    def test_an_adjustment_of_the_wrong_amount_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units != 0)
        result = check_one(data, resolved_tools(data, corruption="wrong_amount"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.ADJUSTMENT_TOTAL_MISMATCH in {m.code for m in result.mismatches}

    def test_an_adjustment_in_the_wrong_currency_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units != 0)
        result = check_one(data, resolved_tools(data, corruption="wrong_currency"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.ADJUSTMENT_CURRENCY_MISMATCH in {m.code for m in result.mismatches}

    def test_an_adjustment_with_the_wrong_reason_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units != 0)
        result = check_one(data, resolved_tools(data, corruption="wrong_reason"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.ADJUSTMENT_REASON_MISMATCH in {m.code for m in result.mismatches}

    def test_a_missing_adjustment_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units != 0)
        result = check_one(data, resolved_tools(data, corruption="skip_adjustment"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.ADJUSTMENT_TOTAL_MISMATCH in {m.code for m in result.mismatches}

    def test_a_double_posted_adjustment_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units != 0)
        result = check_one(data, resolved_tools(data, corruption="double_post"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.UNEXPECTED_ADJUSTMENT in {m.code for m in result.mismatches}

    def test_an_adjustment_where_none_was_expected_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.adjustment_minor_units == 0)
        result = check_one(data, resolved_tools(data, corruption="unexpected_adjustment"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.UNEXPECTED_ADJUSTMENT in {m.code for m in result.mismatches}

    def test_failing_to_void_a_duplicate_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.voided_bank_line_id is not None)
        result = check_one(data, resolved_tools(data, corruption="skip_void"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.VOIDED_LINE_MISMATCH in {m.code for m in result.mismatches}

    def test_voiding_a_line_that_should_have_survived_fails(self):
        data = dataset()
        position = index_of(data, lambda end: end.voided_bank_line_id is None)
        result = check_one(data, resolved_tools(data, corruption="extra_void"), position)
        assert result.verdict is CheckerVerdict.FAIL
        assert MismatchCode.VOIDED_LINE_MISMATCH in {m.code for m in result.mismatches}


class TestDeterminism:
    def test_the_same_inputs_produce_identical_result_bytes(self):
        data = dataset()
        tools = resolved_tools(data)
        first = canonical_bytes([r.model_dump(mode="json") for r in check_all(data, tools)])
        for _ in range(5):
            assert (
                canonical_bytes([r.model_dump(mode="json") for r in check_all(data, tools)])
                == first
            )

    def test_mismatch_order_is_stable(self):
        data = dataset()
        tools = resolved_tools(data, corruption="wrong_line")
        first = [m.code for m in check_one(data, tools, 0).mismatches]
        for _ in range(5):
            assert [m.code for m in check_one(data, tools, 0).mismatches] == first


class TestBulkMeasurement:
    def test_five_hundred_correct_resolutions_all_pass(self):
        data = dataset(BULK_COUNT)
        results = check_all(data, resolved_tools(data))
        assert sum(r.verdict is CheckerVerdict.PASS for r in results) == BULK_COUNT

    def test_five_hundred_corrupted_resolutions_all_fail(self):
        data = dataset(BULK_COUNT)
        results = check_all(data, resolved_tools(data, corruption="wrong_line"))
        assert sum(r.verdict is CheckerVerdict.FAIL for r in results) == BULK_COUNT

    def test_five_hundred_untouched_records_are_all_undetermined(self):
        data = dataset(BULK_COUNT)
        results = check_all(data, ReconciliationTools.from_snapshot(data.world))
        assert sum(r.verdict is CheckerVerdict.UNDETERMINED for r in results) == BULK_COUNT
