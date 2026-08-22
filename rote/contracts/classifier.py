from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import ExceptionCategory, UntrustedText

FROZEN = ConfigDict(extra="forbid", frozen=True)
# model_id is a field name here, not a pydantic reserved prefix
NAMED_MODEL = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
FULL_SCALE = 1000


class ClassificationRequest(BaseModel):
    model_config = FROZEN

    task_input: dict[str, Any]
    # its own channel, never concatenated into instructions
    untrusted: tuple[UntrustedText, ...]
    allowed_categories: tuple[ExceptionCategory, ...]


# what a model hands back, before anything downstream is allowed to look at it
class ClassificationResponse(BaseModel):
    model_config = FROZEN

    category: str = Field(min_length=1)
    confidence_per_mille: int = Field(ge=0, le=FULL_SCALE)


# a typed category and nothing else: there is no field here through which a model could act
class Classification(BaseModel):
    model_config = NAMED_MODEL

    category: ExceptionCategory
    confidence_per_mille: int = Field(ge=0, le=FULL_SCALE)
    model_id: str = Field(min_length=1)
    prompt_template_id: str = Field(min_length=1)
    rejected_output: str | None = None


class ClassifierModel(Protocol):
    model_id: str
    prompt_template_id: str
    # D5: any field that can carry merchant free text is local-model-only
    is_local: bool

    def classify(self, request: ClassificationRequest) -> ClassificationResponse: ...
