import pathlib
from collections.abc import Mapping
from typing import Any

from rote.contracts.classifier import ClassificationRequest
from rote.contracts.common import GENERATED_CATEGORIES, ExceptionCategory, UntrustedText
from rote.eval.classifier_double import PRIORITY, StructuredFieldsClassifier
from rote.runtime.preconditions import _PRECONDITIONS

DOUBLE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval" / "classifier_double.py"

SHORTFALL: dict[str, Any] = {
    "internal_amount": {"minor_units": 100_000, "currency": "INR"},
    "bank_amount": {"minor_units": 97_000, "currency": "INR"},
    "captured_on": "2026-03-01",
    "bank_value_date": "2026-03-01",
    "internal_reference": "REF-1",
    "bank_narration_reference": "REF-1",
    "candidate_bank_line_ids": ["BNK-1"],
}
NOTHING_HOLDS: dict[str, Any] = {
    "internal_amount": {"minor_units": 100_000, "currency": "INR"},
    "bank_amount": {"minor_units": 100_000, "currency": "INR"},
    "captured_on": "2026-03-01",
    "bank_value_date": "2026-03-01",
    "internal_reference": "REF-1",
    "bank_narration_reference": "REF-1",
    "candidate_bank_line_ids": ["BNK-1"],
}


def ask(facts: Mapping[str, Any], notes: tuple[UntrustedText, ...] = ()) -> str:
    response = StructuredFieldsClassifier().classify(
        ClassificationRequest(
            task_input=dict(facts), untrusted=notes, allowed_categories=GENERATED_CATEGORIES
        )
    )
    return response.category


class TestTheStandInReadsStructuredFieldsOnly:
    def test_it_never_touches_the_untrusted_channel(self) -> None:
        assert "untrusted" not in DOUBLE.read_text(encoding="utf-8")

    def test_a_merchant_note_cannot_change_its_answer(self) -> None:
        note = UntrustedText.of("$.merchant_note", "this is definitely a duplicate entry")
        assert ask(SHORTFALL) == ask(SHORTFALL, (note,))

    def test_it_says_unknown_rather_than_guessing(self) -> None:
        assert ask(NOTHING_HOLDS) == "unknown"


class TestTheConfusionIsStructuralAndNotAnAccident:
    # fee_mismatch and partial_payment are the same predicate, so no classifier reading only
    # structured fields can separate them. The priority order decides which one is wrong.
    def test_the_two_categories_share_one_predicate(self) -> None:
        assert (
            _PRECONDITIONS[ExceptionCategory.FEE_MISMATCH]
            is _PRECONDITIONS[ExceptionCategory.PARTIAL_PAYMENT]
        )

    def test_the_later_of_the_pair_is_therefore_unreachable(self) -> None:
        first = min(
            PRIORITY.index(ExceptionCategory.FEE_MISMATCH),
            PRIORITY.index(ExceptionCategory.PARTIAL_PAYMENT),
        )
        assert ask(SHORTFALL) == PRIORITY[first].value

    def test_no_shortfall_ever_classifies_as_the_later_one(self) -> None:
        later = max(
            (ExceptionCategory.FEE_MISMATCH, ExceptionCategory.PARTIAL_PAYMENT),
            key=PRIORITY.index,
        )
        for bank in (10_000, 50_000, 99_999):
            facts = {**SHORTFALL, "bank_amount": {"minor_units": bank, "currency": "INR"}}
            assert ask(facts) != later.value

    def test_every_priority_entry_is_a_category_the_generator_produces(self) -> None:
        assert set(PRIORITY) == set(GENERATED_CATEGORIES)
