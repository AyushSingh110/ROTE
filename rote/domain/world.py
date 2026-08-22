from __future__ import annotations

from datetime import date

from rote.contracts.common import Currency, Money
from rote.contracts.errors import RecordNotFoundError, ToolRequestError
from rote.contracts.reconciliation import (
    Adjustment,
    AdjustmentReason,
    BankStatementLine,
    FeeSchedule,
    FxRate,
    SettlementRecord,
    SettlementStatus,
    WorldSnapshot,
)

FxKey = tuple[Currency, Currency, date]


class ReconciliationWorld:
    def __init__(self, snapshot: WorldSnapshot) -> None:
        self._records: dict[str, SettlementRecord] = {
            record.record_id: record for record in snapshot.settlement_records
        }
        self._orders: dict[str, str] = {
            record.order_id: record.record_id for record in snapshot.settlement_records
        }
        self._lines: dict[str, BankStatementLine] = {
            line.line_id: line for line in snapshot.bank_lines
        }
        self._fee_schedules: dict[str, FeeSchedule] = {
            schedule.merchant_id: schedule for schedule in snapshot.fee_schedules
        }
        self._fx_rates: dict[FxKey, FxRate] = {
            (rate.base, rate.quote, rate.rate_date): rate for rate in snapshot.fx_rates
        }
        self._adjustments: list[Adjustment] = list(snapshot.adjustments)
        self._completed: dict[str, tuple[str, str]] = {}

    def snapshot(self) -> WorldSnapshot:
        return WorldSnapshot(
            settlement_records=tuple(self._records.values()),
            bank_lines=tuple(self._lines.values()),
            fee_schedules=tuple(self._fee_schedules.values()),
            fx_rates=tuple(self._fx_rates.values()),
            adjustments=tuple(self._adjustments),
        )

    def get_record(self, record_id: str) -> SettlementRecord:
        record = self._records.get(record_id)
        if record is None:
            raise RecordNotFoundError(f"no settlement record {record_id!r}")
        return record

    def get_line(self, line_id: str) -> BankStatementLine:
        line = self._lines.get(line_id)
        if line is None:
            raise RecordNotFoundError(f"no bank statement line {line_id!r}")
        return line

    def get_fee_schedule(self, merchant_id: str) -> FeeSchedule:
        schedule = self._fee_schedules.get(merchant_id)
        if schedule is None:
            raise RecordNotFoundError(f"no fee schedule for merchant {merchant_id!r}")
        return schedule

    def get_fx_rate(self, base: Currency, quote: Currency, rate_date: date) -> FxRate:
        rate = self._fx_rates.get((base, quote, rate_date))
        if rate is None:
            raise RecordNotFoundError(f"no {base}/{quote} rate for {rate_date.isoformat()}")
        return rate

    def find_lines_by_amount(
        self, minor_units: int, currency: Currency, around_date: date, window_days: int
    ) -> tuple[str, ...]:
        return tuple(
            line.line_id
            for line in self._lines.values()
            if not line.voided
            and line.amount.minor_units == minor_units
            and line.amount.currency is currency
            and abs((line.value_date - around_date).days) <= window_days
        )

    def find_lines_by_reference(self, reference: str) -> tuple[str, ...]:
        return tuple(
            line.line_id
            for line in self._lines.values()
            if not line.voided and line.narration_reference == reference
        )

    def lines_in_batch(self, batch_id: str) -> tuple[BankStatementLine, ...]:
        found = tuple(line for line in self._lines.values() if line.batch_id == batch_id)
        if not found:
            raise RecordNotFoundError(f"no bank statement batch {batch_id!r}")
        return found

    def merchant_index(self, merchant_id: str) -> int:
        if merchant_id not in self._fee_schedules:
            raise RecordNotFoundError(f"no merchant {merchant_id!r}")
        return int(merchant_id.rsplit("-", maxsplit=1)[-1])

    def order_index(self, order_id: str) -> int:
        if order_id not in self._orders:
            raise RecordNotFoundError(f"no order {order_id!r}")
        return int(order_id.rsplit("-", maxsplit=1)[-1])

    def post_adjustment(
        self,
        *,
        record_id: str,
        amount: Money,
        reason: AdjustmentReason,
        idempotency_key: str,
        action_fingerprint: str,
    ) -> Adjustment:
        replayed = self._replayed_entity_id(idempotency_key, action_fingerprint)
        if replayed is not None:
            return self._adjustment(replayed)
        self.get_record(record_id)
        adjustment = Adjustment(
            adjustment_id=f"ADJ-{len(self._adjustments):06d}",
            record_id=record_id,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        self._adjustments.append(adjustment)
        self._completed[idempotency_key] = (action_fingerprint, adjustment.adjustment_id)
        return adjustment

    def close_settlement(
        self,
        *,
        record_id: str,
        bank_line_id: str,
        status: SettlementStatus,
        idempotency_key: str,
        action_fingerprint: str,
    ) -> SettlementRecord:
        replayed = self._replayed_entity_id(idempotency_key, action_fingerprint)
        if replayed is not None:
            return self.get_record(replayed)
        if status is SettlementStatus.UNMATCHED:
            raise ToolRequestError("a settlement cannot be closed as unmatched")
        record = self.get_record(record_id)
        self.get_line(bank_line_id)
        updated = record.model_copy(update={"status": status, "matched_bank_line_id": bank_line_id})
        self._records[record_id] = updated
        self._completed[idempotency_key] = (action_fingerprint, record_id)
        return updated

    def void_line(
        self, *, line_id: str, idempotency_key: str, action_fingerprint: str
    ) -> BankStatementLine:
        replayed = self._replayed_entity_id(idempotency_key, action_fingerprint)
        if replayed is not None:
            return self.get_line(replayed)
        line = self.get_line(line_id)
        voided = line.model_copy(update={"voided": True})
        self._lines[line_id] = voided
        self._completed[idempotency_key] = (action_fingerprint, line_id)
        return voided

    def _replayed_entity_id(self, idempotency_key: str, action_fingerprint: str) -> str | None:
        prior = self._completed.get(idempotency_key)
        if prior is None:
            return None
        if prior[0] != action_fingerprint:
            raise ToolRequestError(
                f"idempotency key {idempotency_key!r} was already used for a different action"
            )
        return prior[1]

    def _adjustment(self, adjustment_id: str) -> Adjustment:
        for adjustment in self._adjustments:
            if adjustment.adjustment_id == adjustment_id:
                return adjustment
        raise RecordNotFoundError(f"no adjustment {adjustment_id!r}")
