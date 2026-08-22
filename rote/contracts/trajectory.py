from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.fingerprint import DEFAULT_MAX_DEPTH

FROZEN = ConfigDict(extra="forbid", frozen=True)
FINGERPRINT_HEX_LENGTH = 64

Outcome = Literal["resolved", "escalated", "failed"]


class GateVerdict(StrEnum):
    PERMIT = "permit"
    REFUSE = "refuse"
    ESCALATE = "escalate"
    # no gate stood in this call path; a run recorded before the gate existed says so out loud
    UNGATED = "ungated"


class ToolErrorRecord(BaseModel):
    model_config = FROZEN

    kind: str = Field(min_length=1)
    message: str


class TrajectoryStep(BaseModel):
    model_config = FROZEN

    index: int = Field(ge=0)
    tool: str = Field(min_length=1)
    args: dict[str, Any]
    result: dict[str, Any] | None
    result_fingerprint: str = Field(
        min_length=FINGERPRINT_HEX_LENGTH, max_length=FINGERPRINT_HEX_LENGTH
    )
    gate_verdict: GateVerdict
    idempotency_key: str | None
    error: ToolErrorRecord | None
    attempts: int = Field(ge=1)
    latency_ms: int = Field(ge=0)


class Trajectory(BaseModel):
    model_config = FROZEN

    trajectory_id: UUID
    schema_version: Literal[1] = 1
    correlation_id: str = Field(min_length=1)
    domain: Domain
    executor_kind: Literal["live_agent", "plan"]
    task_input_redacted: dict[str, Any]
    untrusted_text_paths: tuple[str, ...]
    category: ExceptionCategory | None
    category_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    steps: tuple[TrajectoryStep, ...]
    outcome: Outcome
    checker_verdict: CheckerVerdict | None
    checker_version: str | None
    agent_model_id: str = Field(min_length=1)
    prompt_template_id: str = Field(min_length=1)
    dry_run: bool
    started_at: datetime
    finished_at: datetime
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _reject_naive_datetime(cls, moment: datetime) -> datetime:
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError("trajectory timestamps must carry a timezone")
        return moment

    @model_validator(mode="after")
    def _reject_incoherent_trajectory(self) -> Trajectory:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        if [step.index for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("step indices must be dense and ordered from zero")
        if (self.checker_verdict is None) != (self.checker_version is None):
            raise ValueError("a checker verdict and its version are recorded together or not")
        return self


class TrajectoryStore(Protocol):
    def append(self, trajectory: Trajectory) -> None: ...

    def all(self) -> tuple[Trajectory, ...]: ...

    def count(self) -> int: ...


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "GateVerdict",
    "Outcome",
    "ToolErrorRecord",
    "Trajectory",
    "TrajectoryStep",
    "TrajectoryStore",
]
