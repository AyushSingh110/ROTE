from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Domain, UntrustedText

FROZEN = ConfigDict(extra="forbid", frozen=True)


# the split happens once, here, and nothing downstream can put the two halves back together
class TaskInput(BaseModel):
    model_config = FROZEN

    correlation_id: str = Field(min_length=1)
    domain: Domain
    structured: dict[str, Any]
    untrusted: tuple[UntrustedText, ...]
    redactions: tuple[str, ...] = ()
