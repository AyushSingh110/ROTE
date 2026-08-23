from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.plan import Plan

FROZEN = ConfigDict(extra="forbid", frozen=True)


class RouteKind(StrEnum):
    COMPILED_PLAN = "compiled_plan"
    LIVE_AGENT = "live_agent"


class RouteReason(StrEnum):
    PLAN_MATCHED = "plan_matched"
    UNKNOWN_CATEGORY = "unknown_category"
    LOW_CONFIDENCE = "low_classifier_confidence"
    PRECONDITION_CONTRADICTION = "precondition_contradiction"
    # more than one procedure fits the same evidence, so the evidence does not choose one
    AMBIGUOUS_EVIDENCE = "ambiguous_evidence"
    NO_ACTIVE_PLAN = "no_plan"


class Route(BaseModel):
    model_config = FROZEN

    kind: RouteKind
    reason: RouteReason
    plan_id: str | None = None
    plan_version: int | None = None
    detail: str = ""


# the registry satisfies this; the router never imports the offline compiler to get at it
class PlanSource(Protocol):
    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None: ...
