"""Kept so the evaluation experiments keep their import path.

The implementation moved to rote.runtime.evidence_check: checking evidence against the
authoritative record before granting authority is a runtime concern, and the service layer
may not import the evaluator. Re-exported rather than duplicated so the two cannot drift.
"""

from rote.runtime.evidence_check import (
    FieldCheck,
    VerificationOutcome,
    VerificationResult,
    verify,
)

__all__ = ["FieldCheck", "VerificationOutcome", "VerificationResult", "verify"]
