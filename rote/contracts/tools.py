from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    mutating: bool
    parameters: dict[str, Any]


# the boundary the agent and the executor both talk to; the policy gate will implement it,
# so neither can reach an adapter directly and neither can see a tool the boundary withheld
@runtime_checkable
class Toolbox(Protocol):
    # a boundary states whether it enforces policy, so a recorded verdict is never a guess
    enforces_policy: bool

    def available_tools(self) -> tuple[ToolSpec, ...]: ...

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
