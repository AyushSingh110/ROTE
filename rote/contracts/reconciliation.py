from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Currency, Domain, ExceptionCategory, Money, UntrustedText

FROZEN = ConfigDict(extra="forbid", frozen=True)


class SettlementStatus(StrEnum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PARTIALLY_SETTLED = "partially_settled"


class AdjustmentReason(StrEnum):
    FEE = "fee"
    FX_ROUNDING = "fx_rounding"
    SHORTFALL = "shortfall"


class SettlementRecord(BaseModel):
    model_config = FROZEN

    record_id: str
    order_id: str
    merchant_id: str
    amount: Money
    reference: str
    captured_on: date
    status: SettlementStatus
    matched_bank_line_id: str | None = None


class BankStatementLine(BaseModel):
    model_config = FROZEN

    line_id: str
    batch_id: str
    amount: Money
    value_date: date
    narration_reference: str
    voided: bool = False


class FeeSchedule(BaseModel):
    model_config = FROZEN

    merchant_id: str
    currency: Currency
    flat_fee_minor_units: int = Field(ge=0)
    percentage_bps: int = Field(ge=0)


class FxRate(BaseModel):
    model_config = FROZEN

    base: Currency
    quote: Currency
    rate_date: date
    rate_micros: int = Field(gt=0)


class Adjustment(BaseModel):
    model_config = FROZEN

    adjustment_id: str
    record_id: str
    amount: Money
    reason: AdjustmentReason
    idempotency_key: str


class WorldSnapshot(BaseModel):
    model_config = FROZEN

    settlement_records: tuple[SettlementRecord, ...]
    bank_lines: tuple[BankStatementLine, ...]
    fee_schedules: tuple[FeeSchedule, ...]
    fx_rates: tuple[FxRate, ...]
    adjustments: tuple[Adjustment, ...] = ()


class ReconciliationFacts(BaseModel):
    model_config = FROZEN

    exception_id: str
    record_id: str
    merchant_id: str
    internal_amount: Money
    internal_reference: str
    captured_on: date
    candidate_bank_line_ids: tuple[str, ...]
    bank_amount: Money | None
    bank_value_date: date | None
    bank_narration_reference: str | None


class ReconciliationException(BaseModel):
    model_config = FROZEN

    exception_id: str
    domain: Domain
    facts: ReconciliationFacts
    untrusted: tuple[UntrustedText, ...]


class ExpectedEndState(BaseModel):
    model_config = FROZEN

    settlement_status: SettlementStatus
    matched_bank_line_id: str | None
    voided_bank_line_id: str | None
    # signed so that bank_amount + adjustment_minor_units == internal_amount
    adjustment_minor_units: int
    adjustment_currency: Currency | None
    adjustment_reason: AdjustmentReason | None


class GroundTruth(BaseModel):
    model_config = FROZEN

    exception_id: str
    category: ExceptionCategory
    expected_end_state: ExpectedEndState


class GeneratedDataset(BaseModel):
    model_config = FROZEN

    seed: int
    world: WorldSnapshot
    exceptions: tuple[ReconciliationException, ...]
    ground_truths: tuple[GroundTruth, ...]
