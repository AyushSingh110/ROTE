"""Kept so the frozen Phase 16 evaluation keeps its import path.

The implementation moved to rote.runtime.classifier_rules: it is a deterministic,
model-free classifier over tested preconditions, so it is a runtime component rather
than an evaluation artifact. Re-exported here rather than duplicated, so the evaluator
and the live runtime can never drift apart.
"""

from rote.runtime.classifier_rules import (
    FIXED_CONFIDENCE_PER_MILLE,
    MODEL_ID,
    PRIORITY,
    PROMPT_TEMPLATE_ID,
    StructuredFieldsClassifier,
)

__all__ = [
    "FIXED_CONFIDENCE_PER_MILLE",
    "MODEL_ID",
    "PRIORITY",
    "PROMPT_TEMPLATE_ID",
    "StructuredFieldsClassifier",
]
