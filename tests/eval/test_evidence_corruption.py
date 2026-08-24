import ast
import pathlib

import pytest

from rote.contracts.common import GENERATED_CATEGORIES, Currency, ExceptionCategory
from rote.eval.evidence_corruption import EvidenceError, corrupt, corrupted_dataset
from rote.runtime.preconditions import precondition_holds
from rote.service.scenario import demo_dataset

EVAL = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
FORBIDDEN = (
    "rote.runtime.router",
    "rote.runtime.guard",
    "rote.runtime.executor",
    "rote.safety",
    "rote.compiler.registry",
    "rote.service",
    "rote.web",
)

DATA = demo_dataset()
TRUTH = {t.exception_id: t.category for t in DATA.ground_truths}
CASES = {e.exception_id: e for e in DATA.exceptions}


def a_case(category: ExceptionCategory):  # type: ignore[no-untyped-def]
    for exception in DATA.exceptions:
        if TRUTH[exception.exception_id] is category:
            return exception
    raise LookupError(category)


def fitting(facts) -> list[str]:  # type: ignore[no-untyped-def]
    payload = facts.model_dump(mode="json")
    return sorted(c.value for c in GENERATED_CATEGORIES if precondition_holds(c, payload))


class TestTheLayerSitsOutsideRote:
    def test_it_imports_no_decision_or_safety_component(self) -> None:
        tree = ast.parse((EVAL / "evidence_corruption.py").read_text(encoding="utf-8"))
        reached = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                reached.add(node.module)
            elif isinstance(node, ast.Import):
                reached.update(a.name for a in node.names)
        assert not any(name.startswith(FORBIDDEN) for name in reached), sorted(reached)

    # Rote must not be able to tell a corrupted case from a genuine one
    def test_the_corrupted_facts_carry_no_marker(self) -> None:
        exception = a_case(ExceptionCategory.PARTIAL_PAYMENT)
        after, _ = corrupt(exception.facts, EvidenceError.AMOUNT_OFF_BY_ONE, TRUTH)
        assert set(after.model_dump()) == set(exception.facts.model_dump())


class TestCorruptionIsDeterministicAndPreservesTheOriginal:
    @pytest.mark.parametrize("error", list(EvidenceError))
    def test_the_same_case_corrupts_identically_every_time(self, error: EvidenceError) -> None:
        exception = a_case(ExceptionCategory.PARTIAL_PAYMENT)
        results = {corrupt(exception.facts, error, TRUTH)[0] for _ in range(5)}
        assert len(results) == 1

    @pytest.mark.parametrize("error", list(EvidenceError))
    def test_the_original_evidence_is_never_mutated(self, error: EvidenceError) -> None:
        exception = a_case(ExceptionCategory.FX_ROUNDING)
        before = exception.facts.model_dump(mode="json")
        corrupt(exception.facts, error, TRUTH)
        assert exception.facts.model_dump(mode="json") == before

    @pytest.mark.parametrize("error", list(EvidenceError))
    def test_every_corruption_stays_schema_valid(self, error: EvidenceError) -> None:
        for category in GENERATED_CATEGORIES:
            after, _ = corrupt(a_case(category).facts, error, TRUTH)
            assert after.model_validate(after.model_dump()) == after


class TestEachClassBehavesAsPreRegistered:
    def test_none_changes_nothing(self) -> None:
        exception = a_case(ExceptionCategory.TIMING_CUTOFF)
        after, applied = corrupt(exception.facts, EvidenceError.NONE, TRUTH)
        assert after == exception.facts
        assert applied is False

    def test_off_by_one_moves_the_bank_amount_by_exactly_one(self) -> None:
        exception = a_case(ExceptionCategory.FEE_MISMATCH)
        after, applied = corrupt(exception.facts, EvidenceError.AMOUNT_OFF_BY_ONE, TRUTH)
        assert applied is True
        assert after.bank_amount is not None and exception.facts.bank_amount is not None
        assert after.bank_amount.minor_units == exception.facts.bank_amount.minor_units + 1

    def test_a_plausible_shift_stays_positive_and_valid(self) -> None:
        exception = a_case(ExceptionCategory.FEE_MISMATCH)
        after, applied = corrupt(exception.facts, EvidenceError.AMOUNT_PLAUSIBLE_SHIFT, TRUTH)
        assert applied is True
        assert after.bank_amount is not None
        assert after.bank_amount.minor_units > 0

    # the evidence carries no settlement status, so the nearest valid enum is substituted
    def test_currency_substitution_yields_a_different_valid_currency(self) -> None:
        exception = a_case(ExceptionCategory.FEE_MISMATCH)
        after, applied = corrupt(exception.facts, EvidenceError.CURRENCY_SUBSTITUTION, TRUTH)
        assert applied is True
        assert after.bank_amount is not None and exception.facts.bank_amount is not None
        assert after.bank_amount.currency is not exception.facts.bank_amount.currency
        assert after.bank_amount.currency in set(Currency)

    def test_reference_substitution_keeps_a_structurally_valid_reference(self) -> None:
        exception = a_case(ExceptionCategory.TRANSPOSED_REFERENCE)
        after, applied = corrupt(exception.facts, EvidenceError.REFERENCE_SUBSTITUTION, TRUTH)
        assert applied is True
        assert after.bank_narration_reference != exception.facts.bank_narration_reference
        assert isinstance(after.bank_narration_reference, str)
        assert after.bank_narration_reference

    def test_a_timestamp_shift_stays_a_valid_date(self) -> None:
        exception = a_case(ExceptionCategory.TIMING_CUTOFF)
        after, applied = corrupt(exception.facts, EvidenceError.TIMESTAMP_SHIFT, TRUTH)
        assert applied is True
        assert after.bank_value_date is not None
        assert after.bank_value_date != exception.facts.bank_value_date

    def test_a_missing_field_is_absent_rather_than_malformed(self) -> None:
        exception = a_case(ExceptionCategory.FEE_MISMATCH)
        after, applied = corrupt(exception.facts, EvidenceError.MISSING_FIELD, TRUTH)
        assert applied is True
        assert after.bank_amount is None

    def test_the_unread_field_control_touches_nothing_a_precondition_reads(self) -> None:
        for category in GENERATED_CATEGORIES:
            facts = a_case(category).facts
            after, applied = corrupt(facts, EvidenceError.UNREAD_FIELD, TRUTH)
            assert applied is True
            assert after.merchant_id != facts.merchant_id
            assert fitting(after) == fitting(facts)


class TestTheCriticalCase:
    # G: the corrupted evidence must uniquely satisfy a category that is NOT the true one
    @pytest.mark.parametrize("category", list(GENERATED_CATEGORIES))
    def test_cross_category_evidence_fits_exactly_one_wrong_category(
        self, category: ExceptionCategory
    ) -> None:
        exception = a_case(category)
        after, applied = corrupt(exception.facts, EvidenceError.CROSS_CATEGORY, TRUTH)
        assert applied is True
        fits = fitting(after)
        assert len(fits) == 1, f"{category.value} -> {fits}"
        assert fits[0] != category.value

    def test_it_remains_internally_plausible(self) -> None:
        exception = a_case(ExceptionCategory.PARTIAL_PAYMENT)
        after, _ = corrupt(exception.facts, EvidenceError.CROSS_CATEGORY, TRUTH)
        assert after.exception_id == exception.facts.exception_id
        assert after.record_id == exception.facts.record_id
        assert after.candidate_bank_line_ids == exception.facts.candidate_bank_line_ids


class TestTheCorruptedDataset:
    def test_it_preserves_the_world_and_the_ground_truth(self) -> None:
        corrupted = corrupted_dataset(DATA, EvidenceError.CROSS_CATEGORY, TRUTH)
        assert corrupted.world == DATA.world
        assert corrupted.ground_truths == DATA.ground_truths
        assert len(corrupted.exceptions) == len(DATA.exceptions)

    def test_it_replaces_the_evidence_rote_will_see(self) -> None:
        corrupted = corrupted_dataset(DATA, EvidenceError.CROSS_CATEGORY, TRUTH)
        changed = sum(
            1
            for before, after in zip(DATA.exceptions, corrupted.exceptions, strict=True)
            if before.facts != after.facts
        )
        assert changed == len(DATA.exceptions)

    def test_a_clean_dataset_is_unchanged(self) -> None:
        assert corrupted_dataset(DATA, EvidenceError.NONE, TRUTH).exceptions == DATA.exceptions

    def test_the_untrusted_note_is_carried_across_untouched(self) -> None:
        corrupted = corrupted_dataset(DATA, EvidenceError.AMOUNT_OFF_BY_ONE, TRUTH)
        for before, after in zip(DATA.exceptions, corrupted.exceptions, strict=True):
            assert before.untrusted == after.untrusted
