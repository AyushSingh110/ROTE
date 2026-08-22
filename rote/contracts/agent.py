from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rote.contracts.common import UntrustedText
from rote.contracts.tools import ToolSpec

FROZEN = ConfigDict(extra="forbid", frozen=True)


class AgentDecision(BaseModel):
    model_config = FROZEN

    action: Literal["call_tool", "finish", "escalate"]
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _reject_shapes_that_do_not_match_the_action(self) -> AgentDecision:
        if self.action == "call_tool":
            if not self.tool or self.arguments is None:
                raise ValueError("a call_tool decision needs both a tool and arguments")
            return self
        if self.tool is not None or self.arguments is not None:
            raise ValueError(f"a {self.action} decision must not carry a tool call")
        return self


class Observation(BaseModel):
    model_config = FROZEN

    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None


class DecisionRequest(BaseModel):
    model_config = FROZEN

    task_input: dict[str, Any]
    # kept as its own field so untrusted text can never share a channel with instructions
    untrusted: tuple[UntrustedText, ...]
    available_tools: tuple[ToolSpec, ...]
    observations: tuple[Observation, ...]
    steps_remaining: int = Field(ge=0)


class ModelResponse(BaseModel):
    model_config = FROZEN

    decision: AgentDecision
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class AgentBudget(BaseModel):
    model_config = FROZEN

    max_steps: int = Field(gt=0)
    max_tool_errors: int = Field(gt=0)


class LanguageModel(Protocol):
    model_id: str
    prompt_template_id: str

    def decide(self, request: DecisionRequest) -> ModelResponse: ...
