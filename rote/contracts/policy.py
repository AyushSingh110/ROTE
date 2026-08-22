from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Currency, ExceptionCategory

FROZEN = ConfigDict(extra="forbid", frozen=True)


class ExecutionPath(StrEnum):
    LIVE_AGENT = "live_agent"
    COMPILED_PLAN = "compiled_plan"


class PolicyContext(BaseModel):
    model_config = FROZEN

    task_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    path: ExecutionPath
    category: ExceptionCategory | None
    actor: str = Field(min_length=1)


class MoneyArgument(BaseModel):
    model_config = FROZEN

    tool: str = Field(min_length=1)
    amount_arg: str = Field(min_length=1)
    currency_arg: str = Field(min_length=1)


class PolicyRule(BaseModel):
    model_config = FROZEN

    path: ExecutionPath
    # None means "any category on this path", and is the rule used when the category is unknown
    category: ExceptionCategory | None
    allowed_tools: frozenset[str]
    max_per_action: dict[Currency, int]
    max_per_window: dict[Currency, int]
    window_seconds: int = Field(gt=0)


class PolicyConfig(BaseModel):
    model_config = FROZEN

    rules: tuple[PolicyRule, ...]
    money_arguments: tuple[MoneyArgument, ...]
    require_idempotency_for: frozenset[str]

    def rule_for(
        self, path: ExecutionPath, category: ExceptionCategory | None
    ) -> PolicyRule | None:
        if category is not None:
            for rule in self.rules:
                if rule.path is path and rule.category is category:
                    return rule
        for rule in self.rules:
            if rule.path is path and rule.category is None:
                return rule
        return None

    def money_argument_for(self, tool: str) -> MoneyArgument | None:
        for argument in self.money_arguments:
            if argument.tool == tool:
                return argument
        return None
