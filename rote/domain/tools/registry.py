from __future__ import annotations

from pydantic import BaseModel

from rote.domain.tools.contracts import (
    FindBankLinesByAmountRequest,
    GetBankLineRequest,
    GetChargebackHistoryRequest,
    GetFeeScheduleRequest,
    GetFxRateRequest,
    GetMerchantProfileRequest,
    GetSettlementRecordRequest,
    ListBankLinesForReferenceRequest,
    MarkSettlementMatchedRequest,
    PostAdjustmentRequest,
    RecalculateSettlementBatchRequest,
    VoidDuplicateBankLineRequest,
)

# deliberately a superset of what any one category needs, so a recorded tool choice is a real choice
TOOL_REQUESTS: dict[str, type[BaseModel]] = {
    "find_bank_lines_by_amount": FindBankLinesByAmountRequest,
    "get_bank_line": GetBankLineRequest,
    "get_chargeback_history": GetChargebackHistoryRequest,
    "get_fee_schedule": GetFeeScheduleRequest,
    "get_fx_rate": GetFxRateRequest,
    "get_merchant_profile": GetMerchantProfileRequest,
    "get_settlement_record": GetSettlementRecordRequest,
    "list_bank_lines_for_reference": ListBankLinesForReferenceRequest,
    "mark_settlement_matched": MarkSettlementMatchedRequest,
    "post_adjustment": PostAdjustmentRequest,
    "recalculate_settlement_batch": RecalculateSettlementBatchRequest,
    "void_duplicate_bank_line": VoidDuplicateBankLineRequest,
}

TOOL_NAMES: tuple[str, ...] = tuple(sorted(TOOL_REQUESTS))

MUTATING_TOOLS: frozenset[str] = frozenset(
    {"post_adjustment", "mark_settlement_matched", "void_duplicate_bank_line"}
)
