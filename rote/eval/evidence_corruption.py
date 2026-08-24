"""Kept so the corruption experiments keep their import path.

The implementation moved to rote.bootstrap.evidence_corruption: the sandbox demo control
needs it too, and the service layer may not import the evaluator. bootstrap is the layer
both may import. Re-exported rather than duplicated so the two cannot drift.
"""

from rote.bootstrap.evidence_corruption import (
    CURRENCY_ORDER,
    PLAUSIBLE_SHIFT_MINOR_UNITS,
    SUBSTITUTE_LINE,
    SUBSTITUTE_MERCHANT,
    SUBSTITUTE_REFERENCE,
    TIMESTAMP_SHIFT_DAYS,
    EvidenceError,
    corrupt,
    corrupted_dataset,
)

__all__ = [
    "CURRENCY_ORDER",
    "PLAUSIBLE_SHIFT_MINOR_UNITS",
    "SUBSTITUTE_LINE",
    "SUBSTITUTE_MERCHANT",
    "SUBSTITUTE_REFERENCE",
    "TIMESTAMP_SHIFT_DAYS",
    "EvidenceError",
    "corrupt",
    "corrupted_dataset",
]
