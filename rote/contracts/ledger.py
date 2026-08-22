from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rote.contracts.canonical import canonical_bytes
from rote.contracts.errors import CanonicalisationError

HASH_HEX_LENGTH = 64


class LedgerEventType(StrEnum):
    CLASSIFIED = "classified"
    ROUTED = "routed"
    INTENT = "intent"
    OUTCOME = "outcome"
    UNKNOWN = "unknown"
    GATE_VERDICT = "gate_verdict"
    DIVERGENCE = "divergence"
    HANDOVER = "handover"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    PLAN_VALIDATED = "plan_validated"
    PLAN_SHADOWED = "plan_shadowed"
    PLAN_ACTIVATED = "plan_activated"
    PLAN_DEACTIVATED = "plan_deactivated"
    PLAN_RETIRED = "plan_retired"


class LedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    event_type: LedgerEventType
    actor: str = Field(min_length=1)
    payload: dict[str, Any]
    dry_run: bool
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _reject_naive_datetime(cls, moment: datetime) -> datetime:
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError("occurred_at must carry a timezone")
        return moment

    @field_validator("payload")
    @classmethod
    def _reject_unhashable_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            canonical_bytes(payload)
        except CanonicalisationError as error:
            raise ValueError(f"payload cannot be canonically hashed: {error}") from error
        return payload


# chain fields live only here, so a caller submitting a LedgerEvent can never forge them
class LedgerEntry(LedgerEvent):
    seq: int = Field(ge=0)
    schema_version: Literal[1] = 1
    prev_hash: str = Field(min_length=HASH_HEX_LENGTH, max_length=HASH_HEX_LENGTH)
    entry_hash: str = Field(min_length=HASH_HEX_LENGTH, max_length=HASH_HEX_LENGTH)
    payload_hash: str = Field(min_length=HASH_HEX_LENGTH, max_length=HASH_HEX_LENGTH)


class ChainVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    entry_count: int
    first_broken_seq: int | None = None
    reason: str | None = None
