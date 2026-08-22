from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Currency, Domain, ExceptionCategory

FROZEN = ConfigDict(extra="forbid", frozen=True)


class BindingKind(StrEnum):
    LITERAL = "literal"
    FROM_INPUT = "from_input"
    FROM_STEP = "from_step"
    FROM_DERIVATION = "from_derivation"
    FROM_RULE = "from_rule"
    FROM_SLOT = "from_slot"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    INACTIVE = "inactive"
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"


class TruncationReason(StrEnum):
    UNBOUND_ARGUMENT = "unbound_argument"
    INCONSISTENT_ARGUMENTS = "inconsistent_arguments"


class DerivationOperand(BaseModel):
    model_config = FROZEN

    kind: Literal[BindingKind.FROM_INPUT, BindingKind.FROM_STEP]
    json_path: str = Field(min_length=1)
    source_step_index: int | None = None


# a plan references a formula by name; it can never contain one, so there is nothing to evaluate
class DerivationCandidate(BaseModel):
    model_config = FROZEN

    derivation_id: str = Field(min_length=1)
    operands: tuple[DerivationOperand, ...]


class ArgBinding(BaseModel):
    model_config = FROZEN

    arg_name: str = Field(min_length=1)
    kind: BindingKind
    literal_value: Any = None
    json_path: str | None = None
    source_step_index: int | None = None
    derivation: DerivationCandidate | None = None
    # provenance: "inferred from 241 runs at 100% agreement" is what makes a binding defensible
    evidence_run_count: int = Field(ge=1)
    # a coincidence that held for 241 runs is exactly what breaks quietly later, so keep the rest
    alternative_paths: tuple[str, ...] = ()
    alternative_derivations: tuple[DerivationCandidate, ...] = ()


class StepExpectation(BaseModel):
    model_config = FROZEN

    result_fingerprints: frozenset[str]
    numeric_observed: dict[str, tuple[int, int]]
    numeric_widened: dict[str, tuple[int, int]]
    categorical_domains: dict[str, frozenset[str]]
    invariants: tuple[str, ...] = ()
    sample_count: int = Field(ge=1)


class PlanStep(BaseModel):
    model_config = FROZEN

    index: int = Field(ge=0)
    kind: Literal["TOOL_CALL"]
    tool: str = Field(min_length=1)
    args: tuple[ArgBinding, ...]
    expect: StepExpectation
    on_error: Literal["ABORT", "ESCALATE"] = "ESCALATE"


class PolicyRequirement(BaseModel):
    model_config = FROZEN

    allowed_tools: frozenset[str]
    max_per_action: dict[Currency, int]


class ReplayOutcome(BaseModel):
    model_config = FROZEN

    trajectory_id: UUID
    path_equal: bool
    playback_miss: bool
    truncated_at: int | None
    detail: str


class ValidationReport(BaseModel):
    model_config = FROZEN

    holdout_size: int
    path_equal: int
    playback_misses: int
    outcomes: tuple[ReplayOutcome, ...]

    @property
    def passed(self) -> bool:
        return (
            self.holdout_size > 0
            and self.playback_misses == 0
            and self.path_equal == self.holdout_size
        )


class Plan(BaseModel):
    model_config = FROZEN

    plan_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    schema_version: Literal[1] = 1
    domain: Domain
    category: ExceptionCategory
    steps: tuple[PlanStep, ...]
    policy: PolicyRequirement
    status: PlanStatus
    built_from: tuple[UUID, ...]
    compiler_version: str = Field(min_length=1)
    agent_model_id: str = Field(min_length=1)
    skeleton: tuple[str, ...]
    truncated: bool
    truncation_reason: TruncationReason | None
    coverage_count: int = Field(ge=0)
    coverage_total: int = Field(ge=0)
    validation: ValidationReport | None
    activated_by: str | None = None
    activated_at: datetime | None = None
