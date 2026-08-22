from datetime import UTC, datetime, timedelta
from itertools import pairwise

from rote.contracts.canonical import canonical_hash, utc_iso8601
from rote.contracts.ledger import LedgerEntry, LedgerEvent, LedgerEventType
from rote.safety.ledger import GENESIS_HASH, Ledger, entry_hash_of, verify_chain

MOMENT = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_event(index: int = 0, **overrides: object) -> LedgerEvent:
    fields: dict[str, object] = {
        "correlation_id": "corr-1",
        "task_id": f"task-{index}",
        "event_type": LedgerEventType.INTENT,
        "actor": "system:gate",
        "payload": {"action": "post_adjustment", "amount_minor_units": 31750},
        "dry_run": True,
        "occurred_at": MOMENT + timedelta(seconds=index),
    }
    fields.update(overrides)
    return LedgerEvent(**fields)


def filled_ledger(count: int) -> Ledger:
    ledger = Ledger()
    for index in range(count):
        ledger.append(make_event(index))
    return ledger


class TestAppendProducesAValidChain:
    def test_appending_many_entries_verifies(self):
        assert filled_ledger(50).verify().valid is True

    def test_verification_reports_no_broken_sequence(self):
        result = filled_ledger(50).verify()
        assert result.first_broken_seq is None
        assert result.reason is None
        assert result.entry_count == 50

    def test_sequence_numbers_are_dense_and_ordered(self):
        entries = filled_ledger(10).entries
        assert [entry.seq for entry in entries] == list(range(10))

    def test_append_returns_the_sealed_entry(self):
        ledger = Ledger()
        entry = ledger.append(make_event(0))
        assert entry is ledger.entries[0]


class TestLinkage:
    def test_first_entry_points_at_the_genesis_hash(self):
        assert filled_ledger(3).entries[0].prev_hash == GENESIS_HASH

    def test_each_prev_hash_points_at_the_previous_entry_hash(self):
        entries = filled_ledger(5).entries
        for earlier, later in pairwise(entries):
            assert later.prev_hash == earlier.entry_hash

    def test_head_hash_is_the_last_entry_hash(self):
        ledger = filled_ledger(4)
        assert ledger.head_hash == ledger.entries[-1].entry_hash

    def test_payload_hash_is_the_canonical_hash_of_the_payload(self):
        entry = filled_ledger(1).entries[0]
        assert entry.payload_hash == canonical_hash(entry.payload)


class TestHashIsComputedFromTheCanonicalRepresentation:
    def test_entry_hash_matches_the_declared_canonical_body(self):
        entry = filled_ledger(1).entries[0]
        expected_body = {
            "schema_version": entry.schema_version,
            "seq": entry.seq,
            "prev_hash": entry.prev_hash,
            "correlation_id": entry.correlation_id,
            "task_id": entry.task_id,
            "event_type": entry.event_type.value,
            "actor": entry.actor,
            "payload_hash": entry.payload_hash,
            "dry_run": entry.dry_run,
            "occurred_at": utc_iso8601(entry.occurred_at),
        }
        assert entry.entry_hash == canonical_hash(expected_body)

    def test_entry_hash_of_recomputes_the_same_value(self):
        entry = filled_ledger(1).entries[0]
        assert entry_hash_of(entry) == entry.entry_hash

    def test_entry_hash_is_sha256_hex(self):
        entry = filled_ledger(1).entries[0]
        assert len(entry.entry_hash) == 64
        assert set(entry.entry_hash) <= set("0123456789abcdef")


class TestTamperDetection:
    def test_tampering_with_a_payload_is_reported_at_that_entry(self):
        entries = list(filled_ledger(5).entries)
        entries[2] = entries[2].model_copy(update={"payload": {"action": "refund"}})
        result = verify_chain(tuple(entries))
        assert result.valid is False
        assert result.first_broken_seq == 2

    def test_tampering_with_the_actor_is_reported_at_that_entry(self):
        entries = list(filled_ledger(5).entries)
        entries[3] = entries[3].model_copy(update={"actor": "human:forged"})
        result = verify_chain(tuple(entries))
        assert result.first_broken_seq == 3

    def test_tampering_with_the_earliest_entry_is_reported_at_zero(self):
        entries = list(filled_ledger(5).entries)
        entries[0] = entries[0].model_copy(update={"dry_run": False})
        result = verify_chain(tuple(entries))
        assert result.first_broken_seq == 0

    def test_resealing_a_tampered_entry_breaks_the_next_link_instead(self):
        entries = list(filled_ledger(5).entries)
        forged_payload = {"action": "refund", "amount_minor_units": 999999}
        forged = entries[1].model_copy(
            update={"payload": forged_payload, "payload_hash": canonical_hash(forged_payload)}
        )
        entries[1] = forged.model_copy(update={"entry_hash": entry_hash_of(forged)})
        result = verify_chain(tuple(entries))
        assert result.valid is False
        assert result.first_broken_seq == 2

    def test_only_the_first_break_is_reported_when_several_entries_are_tampered(self):
        entries = list(filled_ledger(6).entries)
        entries[4] = entries[4].model_copy(update={"actor": "human:forged"})
        entries[2] = entries[2].model_copy(update={"actor": "human:forged"})
        result = verify_chain(tuple(entries))
        assert result.first_broken_seq == 2

    def test_a_broken_chain_names_a_reason(self):
        entries = list(filled_ledger(3).entries)
        entries[1] = entries[1].model_copy(update={"actor": "human:forged"})
        assert verify_chain(tuple(entries)).reason


class TestOrderingAndDeletion:
    def test_reordering_entries_is_detected(self):
        entries = list(filled_ledger(5).entries)
        entries[1], entries[2] = entries[2], entries[1]
        result = verify_chain(tuple(entries))
        assert result.valid is False
        assert result.first_broken_seq == 1

    def test_removing_an_entry_is_detected(self):
        entries = list(filled_ledger(5).entries)
        del entries[1]
        result = verify_chain(tuple(entries))
        assert result.valid is False
        assert result.first_broken_seq == 1

    def test_removing_the_last_entry_is_not_detectable_by_the_chain_alone(self):
        entries = list(filled_ledger(5).entries)
        del entries[-1]
        assert verify_chain(tuple(entries)).valid is True

    def test_duplicating_an_entry_is_detected(self):
        entries = list(filled_ledger(3).entries)
        entries.insert(1, entries[1])
        result = verify_chain(tuple(entries))
        assert result.valid is False
        assert result.first_broken_seq == 2


class TestAppendOnlyInterface:
    def test_ledger_exposes_no_mutation_operations(self):
        ledger = filled_ledger(2)
        for forbidden in ("update", "delete", "remove", "insert", "pop", "clear", "truncate"):
            assert not hasattr(ledger, forbidden)

    def test_entries_are_returned_as_an_immutable_tuple(self):
        assert isinstance(filled_ledger(2).entries, tuple)

    def test_mutating_the_returned_entries_does_not_affect_the_ledger(self):
        ledger = filled_ledger(3)
        snapshot = list(ledger.entries)
        del snapshot[0]
        assert len(ledger.entries) == 3

    def test_entries_are_frozen_models(self):
        assert all(entry.model_config["frozen"] for entry in filled_ledger(2).entries)


class TestEdgeCases:
    def test_an_empty_ledger_verifies(self):
        ledger = Ledger()
        result = ledger.verify()
        assert result.valid is True
        assert result.entry_count == 0
        assert result.first_broken_seq is None

    def test_an_empty_ledger_head_is_the_genesis_hash(self):
        assert Ledger().head_hash == GENESIS_HASH

    def test_an_empty_ledger_has_no_entries(self):
        assert Ledger().entries == ()
        assert len(Ledger()) == 0

    def test_a_single_entry_ledger_verifies(self):
        ledger = filled_ledger(1)
        assert ledger.verify().valid is True
        assert len(ledger) == 1

    def test_a_single_tampered_entry_is_reported_at_zero(self):
        entries = list(filled_ledger(1).entries)
        entries[0] = entries[0].model_copy(update={"task_id": "task-forged"})
        assert verify_chain(tuple(entries)).first_broken_seq == 0


class TestDeterminism:
    def test_identical_inputs_produce_identical_chains(self):
        first = [entry.entry_hash for entry in filled_ledger(10).entries]
        second = [entry.entry_hash for entry in filled_ledger(10).entries]
        assert first == second

    def test_identical_inputs_produce_identical_head_hashes(self):
        assert filled_ledger(7).head_hash == filled_ledger(7).head_hash

    def test_a_different_payload_produces_a_different_head_hash(self):
        one = Ledger()
        one.append(make_event(0))
        other = Ledger()
        other.append(make_event(0, payload={"action": "post_adjustment", "amount_minor_units": 1}))
        assert one.head_hash != other.head_hash

    def test_payload_key_order_does_not_affect_the_entry_hash(self):
        one = Ledger()
        one.append(make_event(0, payload={"a": 1, "b": 2}))
        other = Ledger()
        other.append(make_event(0, payload={"b": 2, "a": 1}))
        assert one.head_hash == other.head_hash


class TestVerifyChainAcceptsRawSequences:
    def test_verify_chain_works_on_a_plain_tuple_of_entries(self):
        entries: tuple[LedgerEntry, ...] = filled_ledger(4).entries
        assert verify_chain(entries).valid is True

    def test_verify_chain_on_an_empty_tuple_is_valid(self):
        assert verify_chain(()).valid is True
