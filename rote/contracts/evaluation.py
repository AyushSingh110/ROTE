from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rote.contracts.checker import CheckerVerdict

FROZEN = ConfigDict(extra="forbid", frozen=True)
# model_id and plan fields are names here, not pydantic reserved prefixes
NAMED_MODEL = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
FULL_SCALE = 1000


class EvalPath(StrEnum):
    ROTE = "rote"
    LIVE_AGENT = "live_agent"


class TerminalState(StrEnum):
    RESOLVED_COMPILED = "resolved_compiled"
    RESOLVED_LIVE = "resolved_live"
    ESCALATED = "escalated"
    FAILED = "failed"


# one line of the run log: one exception, on one path, with everything SS I needs to recompute
class RunRecord(BaseModel):
    model_config = NAMED_MODEL

    correlation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    seed: int
    path: EvalPath
    terminal_state: TerminalState
    llm_calls_classification: int = Field(ge=0)
    llm_calls_post_classification: int = Field(ge=0)
    route_kind: str | None = None
    route_reason: str | None = None
    escalation_reason: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    checker_verdict: CheckerVerdict
    checker_version: str = Field(min_length=1)
    agent_model_id: str = Field(min_length=1)
    outcome_hash: str = Field(min_length=1)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_ms_total: int = Field(ge=0)
    # the honest one: mocked tools are unfairly fast, so tool time is taken back out
    wall_ms_excluding_tool_io: int = Field(ge=0)
    steps: int = Field(ge=0)

    @model_validator(mode="after")
    def _an_escalation_carries_a_reason_and_nothing_else_does(self) -> RunRecord:
        escalated = self.terminal_state is TerminalState.ESCALATED
        if escalated and not self.escalation_reason:
            raise ValueError("an escalation must name the reason it escalated")
        if not escalated and self.escalation_reason:
            raise ValueError("only an escalation may carry an escalation reason")
        return self


class RepeatRecord(BaseModel):
    model_config = FROZEN

    task_id: str = Field(min_length=1)
    path: EvalPath
    repeat_index: int = Field(ge=0)
    outcome_hash: str = Field(min_length=1)
    plan_id: str | None = None
    slot_call_count: int = Field(ge=0)


class ReplayRecord(BaseModel):
    model_config = FROZEN

    task_id: str = Field(min_length=1)
    original_outcome_hash: str = Field(min_length=1)
    replay_outcome_hash: str = Field(min_length=1)
    idempotency_keys_match: bool
    first_differing_seq: int | None = None
    match: bool | None = None

    @model_validator(mode="after")
    def _a_claimed_match_must_survive_comparing_the_hashes(self) -> ReplayRecord:
        equal = self.original_outcome_hash == self.replay_outcome_hash
        if self.match is not None and self.match is not equal:
            raise ValueError("match must agree with the hashes it claims to summarise")
        return self

    @property
    def reproduced(self) -> bool:
        return self.original_outcome_hash == self.replay_outcome_hash


class ResolutionSummary(BaseModel):
    model_config = FROZEN

    total: int = Field(ge=0)
    resolved_deterministically: int = Field(ge=0)
    resolved_by_the_live_agent: int = Field(ge=0)
    escalated: int = Field(ge=0)
    failed: int = Field(ge=0)
    classification_calls: int = Field(ge=0)
    post_classification_calls: int = Field(ge=0)
    rate_per_mille: int = Field(ge=0, le=FULL_SCALE)


class ConsistencyCohort(BaseModel):
    model_config = FROZEN

    label: str = Field(min_length=1)
    exceptions: int = Field(ge=0)
    repeats_each: int = Field(ge=0)
    single_outcome: int = Field(ge=0)
    max_distinct: int = Field(ge=0)
    rate_per_mille: int = Field(ge=0, le=FULL_SCALE)


class EscalationCount(BaseModel):
    model_config = FROZEN

    reason: str = Field(min_length=1)
    count: int = Field(ge=0)


class AccuracyReport(BaseModel):
    model_config = FROZEN

    tasks: int = Field(ge=0)
    rote_passed: int = Field(ge=0)
    agent_passed: int = Field(ge=0)
    both_pass: int = Field(ge=0)
    both_fail: int = Field(ge=0)
    only_rote: int = Field(ge=0)
    only_agent: int = Field(ge=0)
    undetermined: int = Field(ge=0)


class ReplaySummary(BaseModel):
    model_config = FROZEN

    replayed: int = Field(ge=0)
    reproduced: int = Field(ge=0)
    keys_reproduced: int = Field(ge=0)
    rate_per_mille: int = Field(ge=0, le=FULL_SCALE)


class CostSummary(BaseModel):
    model_config = FROZEN

    path: EvalPath
    runs: int = Field(ge=0)
    median_llm_calls: int = Field(ge=0)
    p95_llm_calls: int = Field(ge=0)
    median_tokens: int = Field(ge=0)
    median_wall_ms: int = Field(ge=0)
    p95_wall_ms: int = Field(ge=0)


__all__ = [
    "AccuracyReport",
    "ConsistencyCohort",
    "CostSummary",
    "EscalationCount",
    "EvalPath",
    "RepeatRecord",
    "ReplayRecord",
    "ReplaySummary",
    "ResolutionSummary",
    "RunRecord",
    "TerminalState",
]
