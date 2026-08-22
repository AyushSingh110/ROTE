from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from rote.contracts.classifier import (
    Classification,
    ClassificationRequest,
    ClassificationResponse,
    ClassifierModel,
)
from rote.contracts.common import GENERATED_CATEGORIES, ExceptionCategory, UntrustedText
from rote.contracts.errors import ClassifierError
from rote.observability.logging import get_logger

_logger = get_logger("rote.runtime.classifier")


# holds no tools and can emit no action: its return type is an enum, so an injected instruction
# has no channel through which to become behaviour
class Classifier:
    def __init__(self, *, model: ClassifierModel) -> None:
        self._model = model

    def classify(
        self,
        task_input: dict[str, Any],
        untrusted: tuple[UntrustedText, ...],
        correlation_id: str = "",
    ) -> Classification:
        if untrusted and not self._model.is_local:
            raise ClassifierError(
                f"{self._model.model_id} is not local, so merchant free text may not reach it"
            )

        response = self._ask(
            ClassificationRequest(
                task_input=task_input,
                untrusted=untrusted,
                allowed_categories=GENERATED_CATEGORIES,
            )
        )
        category = _as_category(response.category)
        if category is None:
            _logger.info(
                "classifier_output_rejected",
                correlation_id=correlation_id,
                model_id=self._model.model_id,
                offered=response.category,
            )
            return Classification(
                category=ExceptionCategory.UNKNOWN,
                confidence_per_mille=0,
                model_id=self._model.model_id,
                prompt_template_id=self._model.prompt_template_id,
                rejected_output=response.category,
            )
        return Classification(
            category=category,
            confidence_per_mille=response.confidence_per_mille,
            model_id=self._model.model_id,
            prompt_template_id=self._model.prompt_template_id,
        )

    def _ask(self, request: ClassificationRequest) -> ClassificationResponse:
        raw = self._model.classify(request)
        try:
            return ClassificationResponse.model_validate(raw)
        except ValidationError as error:
            raise ClassifierError(
                f"{self._model.model_id} returned something that is not a classification: {error}"
            ) from error


def _as_category(name: str) -> ExceptionCategory | None:
    for category in GENERATED_CATEGORIES:
        if category.value == name:
            return category
    return None
