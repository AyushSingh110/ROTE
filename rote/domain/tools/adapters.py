from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from rote.contracts.canonical import canonical_hash
from rote.contracts.common import Money
from rote.contracts.errors import ToolRequestError, UnknownToolError
from rote.contracts.reconciliation import WorldSnapshot
from rote.contracts.tools import ToolSpec
from rote.domain.tools.contracts import (
    FindBankLinesByAmountRequest,
    FindBankLinesByAmountResponse,
    GetBankLineRequest,
    GetBankLineResponse,
    GetChargebackHistoryRequest,
    GetChargebackHistoryResponse,
    GetFeeScheduleRequest,
    GetFeeScheduleResponse,
    GetFxRateRequest,
    GetFxRateResponse,
    GetMerchantProfileRequest,
    GetMerchantProfileResponse,
    GetSettlementRecordRequest,
    GetSettlementRecordResponse,
    ListBankLinesForReferenceRequest,
    ListBankLinesForReferenceResponse,
    MarkSettlementMatchedRequest,
    MarkSettlementMatchedResponse,
    PostAdjustmentRequest,
    PostAdjustmentResponse,
    RecalculateSettlementBatchRequest,
    RecalculateSettlementBatchResponse,
    VoidDuplicateBankLineRequest,
    VoidDuplicateBankLineResponse,
)
from rote.domain.tools.registry import MUTATING_TOOLS, TOOL_NAMES, TOOL_REQUESTS
from rote.domain.world import ReconciliationWorld

MERCHANT_SEGMENTS: tuple[str, ...] = ("small_business", "mid_market", "enterprise")


class ReconciliationTools:
    enforces_policy = False

    def __init__(self, world: ReconciliationWorld) -> None:
        self._world = world
        self._handlers: dict[str, Callable[[Any], BaseModel]] = {
            "find_bank_lines_by_amount": self._find_bank_lines_by_amount,
            "get_bank_line": self._get_bank_line,
            "get_chargeback_history": self._get_chargeback_history,
            "get_fee_schedule": self._get_fee_schedule,
            "get_fx_rate": self._get_fx_rate,
            "get_merchant_profile": self._get_merchant_profile,
            "get_settlement_record": self._get_settlement_record,
            "list_bank_lines_for_reference": self._list_bank_lines_for_reference,
            "mark_settlement_matched": self._mark_settlement_matched,
            "post_adjustment": self._post_adjustment,
            "recalculate_settlement_batch": self._recalculate_settlement_batch,
            "void_duplicate_bank_line": self._void_duplicate_bank_line,
        }

    @classmethod
    def from_snapshot(cls, snapshot: WorldSnapshot) -> ReconciliationTools:
        return cls(ReconciliationWorld(snapshot))

    def snapshot(self) -> WorldSnapshot:
        return self._world.snapshot()

    def available_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=name,
                mutating=name in MUTATING_TOOLS,
                parameters=TOOL_REQUESTS[name].model_json_schema(),
            )
            for name in TOOL_NAMES
        )

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request_model = TOOL_REQUESTS.get(name)
        if request_model is None:
            raise UnknownToolError(f"unknown tool {name!r}")
        try:
            request = request_model.model_validate(dict(payload))
        except ValidationError as error:
            raise ToolRequestError(f"invalid arguments for {name}: {error}") from error
        return self._handlers[name](request).model_dump(mode="json")

    def _get_settlement_record(
        self, request: GetSettlementRecordRequest
    ) -> GetSettlementRecordResponse:
        return GetSettlementRecordResponse(record=self._world.get_record(request.record_id))

    def _get_bank_line(self, request: GetBankLineRequest) -> GetBankLineResponse:
        return GetBankLineResponse(line=self._world.get_line(request.line_id))

    def _find_bank_lines_by_amount(
        self, request: FindBankLinesByAmountRequest
    ) -> FindBankLinesByAmountResponse:
        return FindBankLinesByAmountResponse(
            line_ids=self._world.find_lines_by_amount(
                request.minor_units, request.currency, request.around_date, request.window_days
            )
        )

    def _list_bank_lines_for_reference(
        self, request: ListBankLinesForReferenceRequest
    ) -> ListBankLinesForReferenceResponse:
        return ListBankLinesForReferenceResponse(
            line_ids=self._world.find_lines_by_reference(request.reference)
        )

    def _get_fee_schedule(self, request: GetFeeScheduleRequest) -> GetFeeScheduleResponse:
        return GetFeeScheduleResponse(schedule=self._world.get_fee_schedule(request.merchant_id))

    def _get_fx_rate(self, request: GetFxRateRequest) -> GetFxRateResponse:
        return GetFxRateResponse(
            rate=self._world.get_fx_rate(request.base, request.quote, request.rate_date)
        )

    def _get_merchant_profile(
        self, request: GetMerchantProfileRequest
    ) -> GetMerchantProfileResponse:
        index = self._world.merchant_index(request.merchant_id)
        return GetMerchantProfileResponse(
            merchant_id=request.merchant_id,
            segment=MERCHANT_SEGMENTS[index % len(MERCHANT_SEGMENTS)],
            settlement_cycle_days=1 + index % 3,
        )

    def _get_chargeback_history(
        self, request: GetChargebackHistoryRequest
    ) -> GetChargebackHistoryResponse:
        index = self._world.order_index(request.order_id)
        return GetChargebackHistoryResponse(order_id=request.order_id, chargeback_count=index % 3)

    def _recalculate_settlement_batch(
        self, request: RecalculateSettlementBatchRequest
    ) -> RecalculateSettlementBatchResponse:
        lines = self._world.lines_in_batch(request.batch_id)
        return RecalculateSettlementBatchResponse(
            batch_id=request.batch_id,
            line_count=len(lines),
            total_minor_units=sum(line.amount.minor_units for line in lines),
        )

    def _post_adjustment(self, request: PostAdjustmentRequest) -> PostAdjustmentResponse:
        return PostAdjustmentResponse(
            adjustment=self._world.post_adjustment(
                record_id=request.record_id,
                amount=Money(minor_units=request.minor_units, currency=request.currency),
                reason=request.reason,
                idempotency_key=request.idempotency_key,
                action_fingerprint=_action_fingerprint(request),
            )
        )

    def _mark_settlement_matched(
        self, request: MarkSettlementMatchedRequest
    ) -> MarkSettlementMatchedResponse:
        return MarkSettlementMatchedResponse(
            record=self._world.close_settlement(
                record_id=request.record_id,
                bank_line_id=request.bank_line_id,
                status=request.status,
                idempotency_key=request.idempotency_key,
                action_fingerprint=_action_fingerprint(request),
            )
        )

    def _void_duplicate_bank_line(
        self, request: VoidDuplicateBankLineRequest
    ) -> VoidDuplicateBankLineResponse:
        return VoidDuplicateBankLineResponse(
            line=self._world.void_line(
                line_id=request.line_id,
                idempotency_key=request.idempotency_key,
                action_fingerprint=_action_fingerprint(request),
            )
        )


def _action_fingerprint(request: BaseModel) -> str:
    return canonical_hash(request.model_dump(mode="json"))
