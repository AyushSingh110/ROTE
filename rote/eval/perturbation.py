from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from rote.contracts.classifier import ClassificationRequest, ClassificationResponse
from rote.contracts.common import GENERATED_CATEGORIES, ExceptionCategory
from rote.runtime.classifier_rules import FIXED_CONFIDENCE_PER_MILLE, StructuredFieldsClassifier
from rote.runtime.preconditions import precondition_holds

MODEL_ID = "perturbed-upstream-1"
PROMPT_TEMPLATE_ID = "perturbed-v1"
UNKNOWN_LABEL = "totally_unknown_category"
LOW_CONFIDENCE_PER_MILLE = 100
ORDER: tuple[ExceptionCategory, ...] = tuple(
    sorted(GENERATED_CATEGORIES, key=lambda member: member.value)
)


# the error classes, fixed in writing before any case was run
class UpstreamError(StrEnum):
    NONE = "none"
    ORACLE = "oracle"
    WRONG_CATEGORY = "wrong_category"
    PLAUSIBLE_WRONG = "plausible_wrong"
    UNKNOWN_CATEGORY = "unknown_category"
    LOW_CONFIDENCE = "low_confidence"
    CONTRADICTORY = "contradictory"


# sits upstream of the system under test and emits an ordinary ClassificationResponse. Nothing
# downstream can tell one of these answers from a genuine one, which is the point.
class PerturbedClassifier:
    model_id = MODEL_ID
    prompt_template_id = PROMPT_TEMPLATE_ID
    is_local = True

    def __init__(
        self,
        *,
        inner: StructuredFieldsClassifier,
        error: UpstreamError,
        truth_of: Mapping[str, ExceptionCategory],
    ) -> None:
        self._inner = inner
        self._error = error
        self._truth_of = dict(truth_of)

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        answer = self._inner.classify(request)
        facts = request.task_input
        truth = self._truth_of.get(str(facts.get("exception_id", "")))
        if self._error is UpstreamError.NONE or truth is None:
            return answer
        if self._error is UpstreamError.ORACLE:
            return _say(truth.value)
        if self._error is UpstreamError.UNKNOWN_CATEGORY:
            return _say(UNKNOWN_LABEL)
        if self._error is UpstreamError.LOW_CONFIDENCE:
            return ClassificationResponse(
                category=answer.category, confidence_per_mille=LOW_CONFIDENCE_PER_MILLE
            )
        if self._error is UpstreamError.WRONG_CATEGORY:
            return _say(_next_after(truth).value)
        if self._error is UpstreamError.PLAUSIBLE_WRONG:
            other = _another_fitting(facts, truth)
            return answer if other is None else _say(other.value)
        return _say(_first_refuted(facts).value)


def _say(category: str) -> ClassificationResponse:
    return ClassificationResponse(
        category=category, confidence_per_mille=FIXED_CONFIDENCE_PER_MILLE
    )


def _next_after(truth: ExceptionCategory) -> ExceptionCategory:
    index = ORDER.index(truth)
    return ORDER[(index + 1) % len(ORDER)]


# None when the evidence supports only the true category: there is no plausible alternative,
# and reporting that honestly matters more than forcing every case to be perturbable
def _another_fitting(
    facts: Mapping[str, object], truth: ExceptionCategory
) -> ExceptionCategory | None:
    for member in ORDER:
        if member is not truth and precondition_holds(member, dict(facts)):
            return member
    return None


def _first_refuted(facts: Mapping[str, object]) -> ExceptionCategory:
    for member in ORDER:
        if not precondition_holds(member, dict(facts)):
            return member
    raise LookupError("every category fits these facts, so none can be contradicted")


__all__ = [
    "LOW_CONFIDENCE_PER_MILLE",
    "MODEL_ID",
    "UNKNOWN_LABEL",
    "PerturbedClassifier",
    "UpstreamError",
]
