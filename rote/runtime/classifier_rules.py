from __future__ import annotations

from rote.contracts.classifier import ClassificationRequest, ClassificationResponse
from rote.contracts.common import ExceptionCategory
from rote.runtime.preconditions import precondition_holds

MODEL_ID = "structured-fields-double-1"
PROMPT_TEMPLATE_ID = "structured-only-v1"
FIXED_CONFIDENCE_PER_MILLE = 900

# declared before any accuracy was measured: the most constrained precondition wins, because a
# predicate with more conditions holding is stronger evidence. FEE_MISMATCH and PARTIAL_PAYMENT
# share one predicate, so whichever is listed first always wins and the other is unreachable --
# that is not a bug to fix here, it is the measurement this project exists to make.
PRIORITY: tuple[ExceptionCategory, ...] = (
    ExceptionCategory.TRANSPOSED_REFERENCE,
    ExceptionCategory.DUPLICATE_ENTRY,
    ExceptionCategory.TIMING_CUTOFF,
    ExceptionCategory.FX_ROUNDING,
    ExceptionCategory.FEE_MISMATCH,
    ExceptionCategory.PARTIAL_PAYMENT,
)


# an evaluation stand-in, never part of the system: it lives here so no production package can
# import it, and an import-linter contract proves none does
class StructuredFieldsClassifier:
    model_id = MODEL_ID
    prompt_template_id = PROMPT_TEMPLATE_ID
    is_local = True

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        allowed = set(request.allowed_categories)
        for category in PRIORITY:
            if category in allowed and precondition_holds(category, request.task_input):
                return ClassificationResponse(
                    category=category.value, confidence_per_mille=FIXED_CONFIDENCE_PER_MILLE
                )
        return ClassificationResponse(category="unknown", confidence_per_mille=0)


__all__ = ["PRIORITY", "StructuredFieldsClassifier"]
