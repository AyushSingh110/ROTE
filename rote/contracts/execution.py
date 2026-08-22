from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.canonical import canonical_hash
from rote.contracts.plan import PlanStep

FROZEN = ConfigDict(extra="forbid", frozen=True)


class ExecutionOutcome(StrEnum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class EscalationReason(StrEnum):
    BINDING_UNRESOLVED = "binding_unresolved"
    GATE_NOT_ALLOWLISTED = "gate_not_allowlisted"
    GATE_CAP_EXCEEDED = "gate_cap_exceeded"
    UNKNOWN_ACTION_STATE = "unknown_action_state"
    RESULT_DIVERGENCE = "result_divergence"
    TOOL_ERROR = "tool_error"


class ToolCall(BaseModel):
    model_config = FROZEN

    tool: str
    args: dict[str, Any]


# flat and JSON-serialisable on purpose: mid-run handover is only cheap if the state is data
class ExecutionState(BaseModel):
    model_config = FROZEN

    task_input: dict[str, Any]
    committed: tuple[dict[str, Any], ...]

    def with_committed(self, result: dict[str, Any]) -> ExecutionState:
        return ExecutionState(task_input=self.task_input, committed=(*self.committed, result))


class Handover(BaseModel):
    model_config = FROZEN

    step_index: int = Field(ge=0)
    reason: str
    # the state as it stood BEFORE this step, so a rejected result is not inside it
    state: ExecutionState
    # kept apart and labelled: a diverging tool result is exactly the poisoning vector
    untrusted_result: dict[str, Any] | None


class ResultVerdict(BaseModel):
    model_config = FROZEN

    passed: bool
    reason: str = ""


class ResultInspector(Protocol):
    def inspect(self, step: PlanStep, result: dict[str, Any]) -> ResultVerdict: ...


class ExecutionResult(BaseModel):
    model_config = FROZEN

    plan_id: str
    plan_version: int
    outcome: ExecutionOutcome
    escalation_reason: EscalationReason | None
    steps_completed: int = Field(ge=0)
    calls: tuple[ToolCall, ...]
    handover: Handover | None
    outcome_hash: str


# defined once so every comparison of two runs means the same thing
def outcome_hash(outcome: ExecutionOutcome, calls: Sequence[ToolCall]) -> str:
    return canonical_hash(
        {
            "terminal": outcome.value,
            "calls": [{"tool": call.tool, "args": call.args} for call in calls],
        }
    )
