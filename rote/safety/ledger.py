from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from rote.contracts.canonical import canonical_hash, utc_iso8601
from rote.contracts.ledger import (
    HASH_HEX_LENGTH,
    ChainVerification,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
)

GENESIS_HASH = "0" * HASH_HEX_LENGTH


class Ledger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    @property
    def head_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    def append(self, event: LedgerEvent) -> LedgerEntry:
        seq = len(self._entries)
        prev_hash = self.head_hash
        payload_hash = canonical_hash(event.payload)
        entry_hash = canonical_hash(
            _hash_body(
                schema_version=1,
                seq=seq,
                prev_hash=prev_hash,
                correlation_id=event.correlation_id,
                task_id=event.task_id,
                event_type=event.event_type,
                actor=event.actor,
                payload_hash=payload_hash,
                dry_run=event.dry_run,
                occurred_at=event.occurred_at,
            )
        )
        entry = LedgerEntry(
            **event.model_dump(),
            seq=seq,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            payload_hash=payload_hash,
        )
        self._entries.append(entry)
        return entry

    def verify(self) -> ChainVerification:
        return verify_chain(self.entries)


def entry_hash_of(entry: LedgerEntry) -> str:
    return canonical_hash(
        _hash_body(
            schema_version=entry.schema_version,
            seq=entry.seq,
            prev_hash=entry.prev_hash,
            correlation_id=entry.correlation_id,
            task_id=entry.task_id,
            event_type=entry.event_type,
            actor=entry.actor,
            payload_hash=entry.payload_hash,
            dry_run=entry.dry_run,
            occurred_at=entry.occurred_at,
        )
    )


def verify_chain(entries: Sequence[LedgerEntry]) -> ChainVerification:
    for index, entry in enumerate(entries):
        if entry.seq != index:
            return _broken(entries, index, f"sequence number is {entry.seq}, expected {index}")
        expected_prev = GENESIS_HASH if index == 0 else entries[index - 1].entry_hash
        if entry.prev_hash != expected_prev:
            return _broken(entries, index, "prev_hash does not match the previous entry hash")
        if entry.payload_hash != canonical_hash(entry.payload):
            return _broken(entries, index, "payload does not match payload_hash")
        if entry.entry_hash != entry_hash_of(entry):
            return _broken(entries, index, "entry_hash does not match the entry contents")
    return ChainVerification(valid=True, entry_count=len(entries))


# the one definition of what an entry hash covers; construction and verification both call it
def _hash_body(
    *,
    schema_version: int,
    seq: int,
    prev_hash: str,
    correlation_id: str,
    task_id: str,
    event_type: LedgerEventType,
    actor: str,
    payload_hash: str,
    dry_run: bool,
    occurred_at: datetime,
) -> dict[str, Any]:
    # payload enters via payload_hash so a payload can be redacted later without breaking the chain
    return {
        "schema_version": schema_version,
        "seq": seq,
        "prev_hash": prev_hash,
        "correlation_id": correlation_id,
        "task_id": task_id,
        "event_type": event_type.value,
        "actor": actor,
        "payload_hash": payload_hash,
        "dry_run": dry_run,
        "occurred_at": utc_iso8601(occurred_at),
    }


def _broken(entries: Sequence[LedgerEntry], index: int, reason: str) -> ChainVerification:
    return ChainVerification(
        valid=False, entry_count=len(entries), first_broken_seq=index, reason=reason
    )
