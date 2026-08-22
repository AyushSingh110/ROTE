from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Domain(StrEnum):
    RECONCILIATION = "reconciliation"
    DISPUTE_EVIDENCE = "dispute_evidence"


class ExceptionCategory(StrEnum):
    TIMING_CUTOFF = "timing_cutoff"
    FEE_MISMATCH = "fee_mismatch"
    FX_ROUNDING = "fx_rounding"
    TRANSPOSED_REFERENCE = "transposed_reference"
    PARTIAL_PAYMENT = "partial_payment"
    DUPLICATE_ENTRY = "duplicate_entry"
    UNKNOWN = "unknown"


# UNKNOWN is the classifier's escape hatch and is never produced by the generator
GENERATED_CATEGORIES: tuple[ExceptionCategory, ...] = (
    ExceptionCategory.TIMING_CUTOFF,
    ExceptionCategory.FEE_MISMATCH,
    ExceptionCategory.FX_ROUNDING,
    ExceptionCategory.TRANSPOSED_REFERENCE,
    ExceptionCategory.PARTIAL_PAYMENT,
    ExceptionCategory.DUPLICATE_ENTRY,
)


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minor_units: int
    currency: Currency


class UntrustedText(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: str = Field(min_length=1)
    content: str
    byte_length: int = Field(ge=0)

    @model_validator(mode="after")
    def _reject_mismatched_length(self) -> UntrustedText:
        if self.byte_length != len(self.content.encode()):
            raise ValueError("byte_length does not match content")
        return self

    @classmethod
    def of(cls, source_path: str, content: str) -> UntrustedText:
        return cls(source_path=source_path, content=content, byte_length=len(content.encode()))
