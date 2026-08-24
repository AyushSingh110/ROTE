import ast
import pathlib

import pytest

from rote.contracts.common import ExceptionCategory
from rote.domain.tools.adapters import ReconciliationTools
from rote.eval.evidence_corruption import EvidenceError, corrupt
from rote.eval.verification import VerificationOutcome, verify
from rote.service.scenario import demo_dataset

EVAL = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
FORBIDDEN = (
    "rote.runtime.router",
    "rote.runtime.guard",
    "rote.runtime.executor",
    "rote.safety",
    "rote.compiler",
    "rote.service",
    "rote.web",
)
GROUND_TRUTH_NAMES = ("ground_truth", "GroundTruth", "check_outcome", "CheckerVerdict", "truth_of")

DATA = demo_dataset()
TRUTH = {t.exception_id: t.category for t in DATA.ground_truths}
ADAPTERS = ReconciliationTools.from_snapshot(DATA.world)


def a_case(category: ExceptionCategory):  # type: ignore[no-untyped-def]
    for exception in DATA.exceptions:
        if TRUTH[exception.exception_id] is category:
            return exception
    raise LookupError(category)


class TestTheProbeIsIsolated:
    def test_it_imports_no_decision_or_safety_component(self) -> None:
        tree = ast.parse((EVAL / "verification.py").read_text(encoding="utf-8"))
        reached = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                reached.add(node.module)
            elif isinstance(node, ast.Import):
                reached.update(a.name for a in node.names)
        assert not any(name.startswith(FORBIDDEN) for name in reached), sorted(reached)

    # verification must be decided from evidence alone, never from the answer key
    def test_it_cannot_reach_ground_truth(self) -> None:
        source = (EVAL / "verification.py").read_text(encoding="utf-8")
        for banned in GROUND_TRUTH_NAMES:
            assert banned not in source, banned

    def test_verify_takes_only_facts_and_a_tool_boundary(self) -> None:
        import inspect

        assert list(inspect.signature(verify).parameters) == ["facts", "adapters"]


BROKEN_FIELD = "candidate_bank_line_ids"


class TestCleanEvidenceAgrees:
    # The clean control originally failed: 89 false mismatches, all on candidate_bank_line_ids,
    # all transposed_reference. Cause was a fallback that fired only when the primary query was
    # empty and so returned a partial set. Repaired to a union plus abstention; these tests pin
    # the repaired behaviour, and the failure itself is recorded in the report.
    def test_no_clean_case_produces_a_false_mismatch(self) -> None:
        for exception in DATA.exceptions:
            result = verify(exception.facts, ADAPTERS)
            assert (
                result.mismatched_fields == ()
            ), f"{exception.exception_id}: {result.mismatched_fields}"

    def test_the_abstentions_are_exactly_the_transposed_cases(self) -> None:
        abstained = [
            e.exception_id
            for e in DATA.exceptions
            if verify(e.facts, ADAPTERS).outcome is VerificationOutcome.UNVERIFIABLE
        ]
        assert len(abstained) == 89
        assert {TRUTH[eid].value for eid in abstained} == {"transposed_reference"}

    # abstention must never be silently upgraded to agreement
    def test_an_unconfirmable_candidate_abstains_rather_than_guessing(self) -> None:
        transposed = next(
            e
            for e in DATA.exceptions
            if TRUTH[e.exception_id] is ExceptionCategory.TRANSPOSED_REFERENCE
        )
        result = verify(transposed.facts, ADAPTERS)
        assert result.outcome is VerificationOutcome.UNVERIFIABLE
        assert BROKEN_FIELD in result.unverifiable_fields
        assert BROKEN_FIELD not in result.mismatched_fields

    def test_omitting_a_confirmed_candidate_is_a_mismatch(self) -> None:
        duplicate = next(
            e for e in DATA.exceptions if TRUTH[e.exception_id] is ExceptionCategory.DUPLICATE_ENTRY
        )
        assert len(duplicate.facts.candidate_bank_line_ids) == 2
        truncated = duplicate.facts.model_copy(
            update={"candidate_bank_line_ids": duplicate.facts.candidate_bank_line_ids[:1]}
        )
        assert BROKEN_FIELD in verify(truncated, ADAPTERS).mismatched_fields

    def test_a_clean_case_reports_the_fields_it_checked(self) -> None:
        result = verify(a_case(ExceptionCategory.FX_ROUNDING).facts, ADAPTERS)
        checked = {check.field for check in result.checks}
        assert {"internal_amount", "internal_reference", "captured_on", "merchant_id"} <= checked


class TestCorruptionIsDetected:
    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (EvidenceError.AMOUNT_OFF_BY_ONE, ExceptionCategory.FX_ROUNDING),
            (EvidenceError.CURRENCY_SUBSTITUTION, ExceptionCategory.FEE_MISMATCH),
            (EvidenceError.UNREAD_FIELD, ExceptionCategory.TIMING_CUTOFF),
        ],
    )
    def test_a_corrupted_field_is_reported_as_a_mismatch(
        self, error: EvidenceError, category: ExceptionCategory
    ) -> None:
        corrupted, applied = corrupt(a_case(category).facts, error, {})
        assert applied is True
        result = verify(corrupted, ADAPTERS)
        assert result.outcome is VerificationOutcome.MISMATCH
        assert result.mismatched_fields

    def test_an_absent_field_is_unverifiable_and_never_agreement(self) -> None:
        corrupted, _ = corrupt(
            a_case(ExceptionCategory.FEE_MISMATCH).facts, EvidenceError.MISSING_FIELD, {}
        )
        result = verify(corrupted, ADAPTERS)
        assert result.outcome is not VerificationOutcome.AGREEMENT

    def test_an_unreachable_record_is_unverifiable(self) -> None:
        facts = a_case(ExceptionCategory.FX_ROUNDING).facts.model_copy(
            update={"record_id": "REC-does-not-exist"}
        )
        result = verify(facts, ADAPTERS)
        assert result.outcome is VerificationOutcome.UNVERIFIABLE


class TestDeterminismAndPurity:
    def test_reconstruction_is_deterministic(self) -> None:
        facts = a_case(ExceptionCategory.DUPLICATE_ENTRY).facts
        results = {verify(facts, ADAPTERS).outcome for _ in range(5)}
        assert len(results) == 1

    def test_the_original_facts_are_never_mutated(self) -> None:
        exception = a_case(ExceptionCategory.PARTIAL_PAYMENT)
        before = exception.facts.model_dump(mode="json")
        verify(exception.facts, ADAPTERS)
        assert exception.facts.model_dump(mode="json") == before

    def test_the_probe_does_not_mutate_the_world(self) -> None:
        from rote.contracts.canonical import canonical_hash

        before = canonical_hash(ADAPTERS.snapshot().model_dump(mode="json"))
        for exception in DATA.exceptions[:40]:
            verify(exception.facts, ADAPTERS)
        after = canonical_hash(ADAPTERS.snapshot().model_dump(mode="json"))
        assert before == after
