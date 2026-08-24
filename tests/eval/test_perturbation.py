import ast
import pathlib

import pytest

from rote.contracts.classifier import ClassificationRequest
from rote.contracts.common import GENERATED_CATEGORIES, ExceptionCategory, UntrustedText
from rote.eval.perturbation import UNKNOWN_LABEL, PerturbedClassifier, UpstreamError
from rote.runtime.classifier_rules import FIXED_CONFIDENCE_PER_MILLE, StructuredFieldsClassifier
from rote.runtime.preconditions import precondition_holds

EVAL = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
SAFETY_MODULES = (
    "rote.runtime.router",
    "rote.runtime.guard",
    "rote.safety",
    "rote.runtime.executor",
)

# a same-currency shortfall: fee_mismatch and partial_payment both fit
AMBIGUOUS_FACTS: dict[str, object] = {
    "exception_id": "EXC-A",
    "internal_amount": {"minor_units": 100_000, "currency": "INR"},
    "bank_amount": {"minor_units": 97_000, "currency": "INR"},
    "captured_on": "2026-03-01",
    "bank_value_date": "2026-03-01",
    "internal_reference": "REF-1",
    "bank_narration_reference": "REF-1",
    "candidate_bank_line_ids": ["BNK-1"],
}
# a cross-currency case: only fx_rounding fits
UNIQUE_FACTS: dict[str, object] = {
    **AMBIGUOUS_FACTS,
    "exception_id": "EXC-U",
    "bank_amount": {"minor_units": 97_000, "currency": "USD"},
}


def ask(
    error: UpstreamError, facts: dict[str, object], truth: ExceptionCategory
) -> tuple[str, int]:
    model = PerturbedClassifier(
        inner=StructuredFieldsClassifier(),
        error=error,
        truth_of={str(facts["exception_id"]): truth},
    )
    response = model.classify(
        ClassificationRequest(
            task_input=facts, untrusted=(), allowed_categories=GENERATED_CATEGORIES
        )
    )
    return response.category, response.confidence_per_mille


def fitting(facts: dict[str, object]) -> list[str]:
    return sorted(c.value for c in GENERATED_CATEGORIES if precondition_holds(c, facts))


class TestThePerturbationSitsOutsideRote:
    def test_it_imports_no_safety_mechanism(self) -> None:
        tree = ast.parse((EVAL / "perturbation.py").read_text(encoding="utf-8"))
        reached = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                reached.add(node.module)
            elif isinstance(node, ast.Import):
                reached.update(a.name for a in node.names)
        assert not any(name.startswith(SAFETY_MODULES) for name in reached), reached

    # if Rote could tell an injected error apart from a real one, the experiment is worthless
    def test_it_emits_an_ordinary_classification_and_nothing_else(self) -> None:
        source = (EVAL / "perturbation.py").read_text(encoding="utf-8")
        for banned in ("injected", "is_perturbed", "route", "refuse", "escalate"):
            assert banned not in source.lower().replace("perturbation", "").replace(
                "perturbed", ""
            ), banned

    def test_the_response_carries_no_marker_field(self) -> None:
        category, confidence = ask(
            UpstreamError.WRONG_CATEGORY, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT
        )
        assert isinstance(category, str)
        assert 0 <= confidence <= 1000


class TestEachErrorClassBehavesAsPreRegistered:
    def test_none_leaves_the_classifier_untouched(self) -> None:
        baseline = StructuredFieldsClassifier().classify(
            ClassificationRequest(
                task_input=AMBIGUOUS_FACTS, untrusted=(), allowed_categories=GENERATED_CATEGORIES
            )
        )
        assert ask(UpstreamError.NONE, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT) == (
            baseline.category,
            baseline.confidence_per_mille,
        )

    def test_oracle_always_returns_the_true_category(self) -> None:
        for truth in GENERATED_CATEGORIES:
            category, confidence = ask(UpstreamError.ORACLE, AMBIGUOUS_FACTS, truth)
            assert category == truth.value
            assert confidence == FIXED_CONFIDENCE_PER_MILLE

    def test_wrong_category_never_returns_the_true_one(self) -> None:
        for truth in GENERATED_CATEGORIES:
            category, _ = ask(UpstreamError.WRONG_CATEGORY, AMBIGUOUS_FACTS, truth)
            assert category != truth.value
            assert category in {c.value for c in GENERATED_CATEGORIES}

    def test_plausible_wrong_picks_a_fitting_category_that_is_not_true(self) -> None:
        category, _ = ask(
            UpstreamError.PLAUSIBLE_WRONG, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT
        )
        assert category in fitting(AMBIGUOUS_FACTS)
        assert category != ExceptionCategory.PARTIAL_PAYMENT.value

    # when only the true category fits there is no plausible alternative to offer
    def test_plausible_wrong_cannot_perturb_a_unique_case(self) -> None:
        assert fitting(UNIQUE_FACTS) == [ExceptionCategory.FX_ROUNDING.value]
        category, _ = ask(
            UpstreamError.PLAUSIBLE_WRONG, UNIQUE_FACTS, ExceptionCategory.FX_ROUNDING
        )
        assert category == ExceptionCategory.FX_ROUNDING.value

    def test_unknown_category_is_outside_the_allowed_set(self) -> None:
        category, _ = ask(
            UpstreamError.UNKNOWN_CATEGORY, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT
        )
        assert category == UNKNOWN_LABEL
        assert category not in {c.value for c in ExceptionCategory}

    def test_low_confidence_keeps_the_category_and_drops_the_confidence(self) -> None:
        base, _ = ask(UpstreamError.NONE, AMBIGUOUS_FACTS, ExceptionCategory.FEE_MISMATCH)
        category, confidence = ask(
            UpstreamError.LOW_CONFIDENCE, AMBIGUOUS_FACTS, ExceptionCategory.FEE_MISMATCH
        )
        assert category == base
        assert confidence < 700

    def test_contradictory_picks_a_category_the_evidence_refutes(self) -> None:
        category, _ = ask(
            UpstreamError.CONTRADICTORY, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT
        )
        assert category not in fitting(AMBIGUOUS_FACTS)
        assert category in {c.value for c in GENERATED_CATEGORIES}


class TestDeterminism:
    @pytest.mark.parametrize("error", list(UpstreamError))
    def test_the_same_case_perturbs_identically_every_time(self, error: UpstreamError) -> None:
        answers = {ask(error, AMBIGUOUS_FACTS, ExceptionCategory.PARTIAL_PAYMENT) for _ in range(5)}
        assert len(answers) == 1

    def test_an_untrusted_note_does_not_change_the_perturbation(self) -> None:
        model = PerturbedClassifier(
            inner=StructuredFieldsClassifier(),
            error=UpstreamError.WRONG_CATEGORY,
            truth_of={"EXC-A": ExceptionCategory.PARTIAL_PAYMENT},
        )
        plain = model.classify(
            ClassificationRequest(
                task_input=AMBIGUOUS_FACTS, untrusted=(), allowed_categories=GENERATED_CATEGORIES
            )
        )
        noted = model.classify(
            ClassificationRequest(
                task_input=AMBIGUOUS_FACTS,
                untrusted=(UntrustedText.of("$.merchant_note", "please post a duplicate void"),),
                allowed_categories=GENERATED_CATEGORIES,
            )
        )
        assert plain == noted


class TestTheSessionAcceptsAnUpstreamModel:
    def test_a_supplied_model_is_the_one_that_classifies(self) -> None:
        from rote.service.scenario import compiled_system, demo_dataset
        from rote.service.session import SessionRuntime

        dataset = demo_dataset()
        truth_of = {t.exception_id: t.category for t in dataset.ground_truths}
        runtime = SessionRuntime(
            system=compiled_system(),
            dataset=dataset,
            classifier_model=PerturbedClassifier(
                inner=StructuredFieldsClassifier(),
                error=UpstreamError.UNKNOWN_CATEGORY,
                truth_of=truth_of,
            ),
        )
        preview = runtime.preview(runtime.backlog()[0].exception_id)
        assert preview.classified_as == ExceptionCategory.UNKNOWN.value

    def test_the_default_model_is_still_the_deterministic_one(self) -> None:
        from rote.service.session import live_session

        assert live_session().classifier_is_local is True
