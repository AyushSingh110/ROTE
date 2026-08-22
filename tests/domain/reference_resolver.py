from collections.abc import Callable

from rote.contracts.common import Currency
from rote.contracts.reconciliation import (
    AdjustmentReason,
    GroundTruth,
    ReconciliationException,
    SettlementStatus,
)
from rote.domain.tools.adapters import ReconciliationTools

# test-only oracle: it exists to exercise the checker and must never generate a trajectory,
# because a hand-written correct procedure is exactly what the compiler must not be shown

Corruption = str


def resolve(
    tools: ReconciliationTools,
    exception: ReconciliationException,
    truth: GroundTruth,
    *,
    close_first: bool = False,
    corruption: Corruption | None = None,
    fallback_line_id: str | None = None,
) -> None:
    end = truth.expected_end_state
    key = exception.exception_id
    facts = exception.facts

    matched_line_id = end.matched_bank_line_id
    if corruption == "wrong_line":
        matched_line_id = fallback_line_id

    status = end.settlement_status
    if corruption == "wrong_status":
        status = (
            SettlementStatus.MATCHED
            if status is SettlementStatus.PARTIALLY_SETTLED
            else SettlementStatus.PARTIALLY_SETTLED
        )

    side_effects: list[Callable[[], None]] = []

    if end.adjustment_minor_units != 0 and corruption != "skip_adjustment":
        amount = end.adjustment_minor_units
        currency = end.adjustment_currency
        reason = end.adjustment_reason
        assert currency is not None
        assert reason is not None
        if corruption == "wrong_amount":
            amount += 1
        if corruption == "wrong_currency":
            currency = _other_currency(currency)
        if corruption == "wrong_reason":
            reason = _other_reason(reason)
        side_effects.append(
            lambda: _post(tools, facts.record_id, amount, currency, reason, f"{key}:adjust")
        )
        if corruption == "double_post":
            side_effects.append(
                lambda: _post(
                    tools, facts.record_id, amount, currency, reason, f"{key}:adjust-again"
                )
            )

    if corruption == "unexpected_adjustment" and end.adjustment_minor_units == 0:
        side_effects.append(
            lambda: _post(
                tools,
                facts.record_id,
                1,
                facts.internal_amount.currency,
                AdjustmentReason.FEE,
                f"{key}:surprise",
            )
        )

    void_target = end.voided_bank_line_id
    if corruption == "skip_void":
        void_target = None
    if corruption == "extra_void" and void_target is None:
        void_target = facts.candidate_bank_line_ids[0]
    if void_target is not None:
        target = void_target
        side_effects.append(lambda: _void(tools, target, f"{key}:void"))

    def close() -> None:
        if matched_line_id is None or corruption == "skip_close":
            return
        tools.invoke(
            "mark_settlement_matched",
            {
                "record_id": facts.record_id,
                "bank_line_id": matched_line_id,
                "status": status.value,
                "idempotency_key": f"{key}:close",
            },
        )

    if close_first:
        close()
        for step in reversed(side_effects):
            step()
        return
    for step in side_effects:
        step()
    close()


def _post(
    tools: ReconciliationTools,
    record_id: str,
    minor_units: int,
    currency: Currency,
    reason: AdjustmentReason,
    idempotency_key: str,
) -> None:
    tools.invoke(
        "post_adjustment",
        {
            "record_id": record_id,
            "minor_units": minor_units,
            "currency": currency.value,
            "reason": reason.value,
            "idempotency_key": idempotency_key,
        },
    )


def _other_currency(currency: Currency) -> Currency:
    return Currency.USD if currency is Currency.INR else Currency.INR


def _other_reason(reason: AdjustmentReason) -> AdjustmentReason:
    if reason is AdjustmentReason.FEE:
        return AdjustmentReason.SHORTFALL
    return AdjustmentReason.FEE


def _void(tools: ReconciliationTools, line_id: str, idempotency_key: str) -> None:
    tools.invoke(
        "void_duplicate_bank_line",
        {"line_id": line_id, "idempotency_key": idempotency_key},
    )
