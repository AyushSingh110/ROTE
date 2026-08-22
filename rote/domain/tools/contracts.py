from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Currency
from rote.contracts.reconciliation import (
    Adjustment,
    AdjustmentReason,
    BankStatementLine,
    FeeSchedule,
    FxRate,
    SettlementRecord,
    SettlementStatus,
)

STRICT = ConfigDict(extra="forbid", frozen=True)


class GetSettlementRecordRequest(BaseModel):
    model_config = STRICT
    record_id: str = Field(min_length=1)


class GetSettlementRecordResponse(BaseModel):
    model_config = STRICT
    record: SettlementRecord


class GetBankLineRequest(BaseModel):
    model_config = STRICT
    line_id: str = Field(min_length=1)


class GetBankLineResponse(BaseModel):
    model_config = STRICT
    line: BankStatementLine


class FindBankLinesByAmountRequest(BaseModel):
    model_config = STRICT
    minor_units: int
    currency: Currency
    around_date: date
    window_days: int = Field(ge=0, le=90)


class FindBankLinesByAmountResponse(BaseModel):
    model_config = STRICT
    line_ids: tuple[str, ...]


class ListBankLinesForReferenceRequest(BaseModel):
    model_config = STRICT
    reference: str = Field(min_length=1)


class ListBankLinesForReferenceResponse(BaseModel):
    model_config = STRICT
    line_ids: tuple[str, ...]


class GetFeeScheduleRequest(BaseModel):
    model_config = STRICT
    merchant_id: str = Field(min_length=1)


class GetFeeScheduleResponse(BaseModel):
    model_config = STRICT
    schedule: FeeSchedule


class GetFxRateRequest(BaseModel):
    model_config = STRICT
    base: Currency
    quote: Currency
    rate_date: date


class GetFxRateResponse(BaseModel):
    model_config = STRICT
    rate: FxRate


class GetMerchantProfileRequest(BaseModel):
    model_config = STRICT
    merchant_id: str = Field(min_length=1)


class GetMerchantProfileResponse(BaseModel):
    model_config = STRICT
    merchant_id: str
    segment: str
    settlement_cycle_days: int


class GetChargebackHistoryRequest(BaseModel):
    model_config = STRICT
    order_id: str = Field(min_length=1)


class GetChargebackHistoryResponse(BaseModel):
    model_config = STRICT
    order_id: str
    chargeback_count: int


class RecalculateSettlementBatchRequest(BaseModel):
    model_config = STRICT
    batch_id: str = Field(min_length=1)


class RecalculateSettlementBatchResponse(BaseModel):
    model_config = STRICT
    batch_id: str
    line_count: int
    total_minor_units: int


class PostAdjustmentRequest(BaseModel):
    model_config = STRICT
    record_id: str = Field(min_length=1)
    minor_units: int
    currency: Currency
    reason: AdjustmentReason
    idempotency_key: str = Field(min_length=1)


class PostAdjustmentResponse(BaseModel):
    model_config = STRICT
    adjustment: Adjustment


class MarkSettlementMatchedRequest(BaseModel):
    model_config = STRICT
    record_id: str = Field(min_length=1)
    bank_line_id: str = Field(min_length=1)
    # required, never defaulted: a partial settlement is a decision the resolver has to make
    status: Literal[SettlementStatus.MATCHED, SettlementStatus.PARTIALLY_SETTLED]
    idempotency_key: str = Field(min_length=1)


class MarkSettlementMatchedResponse(BaseModel):
    model_config = STRICT
    record: SettlementRecord


class VoidDuplicateBankLineRequest(BaseModel):
    model_config = STRICT
    line_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class VoidDuplicateBankLineResponse(BaseModel):
    model_config = STRICT
    line: BankStatementLine
