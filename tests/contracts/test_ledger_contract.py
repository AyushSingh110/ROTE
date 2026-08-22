from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rote.contracts.ledger import LedgerEntry, LedgerEvent, LedgerEventType

MOMENT = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_event(**overrides: object) -> LedgerEvent:
    fields: dict[str, object] = {
        "correlation_id": "corr-1",
        "task_id": "task-1",
        "event_type": LedgerEventType.CLASSIFIED,
        "actor": "system:classifier",
        "payload": {"category": "fee_mismatch"},
        "dry_run": True,
        "occurred_at": MOMENT,
    }
    fields.update(overrides)
    return LedgerEvent(**fields)


class TestLedgerEventValidation:
    def test_a_well_formed_event_is_accepted(self):
        assert make_event().event_type is LedgerEventType.CLASSIFIED

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            make_event(surprise="extra")

    def test_unknown_event_type_is_rejected(self):
        with pytest.raises(ValidationError):
            make_event(event_type="not_a_real_event")

    def test_naive_datetime_is_rejected(self):
        with pytest.raises(ValidationError):
            make_event(occurred_at=datetime(2026, 8, 22, 10, 0, 0))

    def test_empty_correlation_id_is_rejected(self):
        with pytest.raises(ValidationError):
            make_event(correlation_id="")

    def test_empty_actor_is_rejected(self):
        with pytest.raises(ValidationError):
            make_event(actor="")

    def test_uncanonicalisable_payload_is_rejected_at_the_boundary(self):
        with pytest.raises(ValidationError):
            make_event(payload={"amount": 12.5})

    def test_payload_with_non_string_keys_is_rejected(self):
        with pytest.raises(ValidationError):
            make_event(payload={1: "one"})

    def test_event_is_frozen(self):
        event = make_event()
        with pytest.raises(ValidationError):
            event.actor = "someone_else"  # type: ignore[misc]


class TestLedgerEntryValidation:
    def test_entry_carries_the_chain_fields(self):
        entry = LedgerEntry(
            **make_event().model_dump(),
            seq=0,
            prev_hash="0" * 64,
            entry_hash="a" * 64,
            payload_hash="b" * 64,
        )
        assert entry.seq == 0
        assert entry.schema_version == 1

    def test_negative_sequence_is_rejected(self):
        with pytest.raises(ValidationError):
            LedgerEntry(
                **make_event().model_dump(),
                seq=-1,
                prev_hash="0" * 64,
                entry_hash="a" * 64,
                payload_hash="b" * 64,
            )

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            LedgerEntry(
                **make_event().model_dump(),
                seq=0,
                prev_hash="0" * 64,
                entry_hash="a" * 64,
                payload_hash="b" * 64,
                surprise="extra",  # type: ignore[call-arg]
            )

    def test_entry_is_frozen(self):
        entry = LedgerEntry(
            **make_event().model_dump(),
            seq=0,
            prev_hash="0" * 64,
            entry_hash="a" * 64,
            payload_hash="b" * 64,
        )
        with pytest.raises(ValidationError):
            entry.seq = 5  # type: ignore[misc]
