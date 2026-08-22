from __future__ import annotations

from typing import Any

from rote.contracts.classifier import Classification
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.plan import PlanStatus
from rote.contracts.routing import PlanSource, Route, RouteKind, RouteReason
from rote.runtime.preconditions import precondition_holds


# no reasoning, no embedding, no model. Biased toward the live agent: a wrong "live" costs
# money, a wrong "plan" costs correctness, and the second is never traded for the first.
class Router:
    def __init__(self, *, plans: PlanSource, domain: Domain, min_confidence_per_mille: int) -> None:
        self._plans = plans
        self._domain = domain
        self._min_confidence = min_confidence_per_mille

    def route(self, facts: dict[str, Any], classification: Classification) -> Route:
        category = classification.category
        if category is ExceptionCategory.UNKNOWN:
            return _live(RouteReason.UNKNOWN_CATEGORY, "no category was established")
        if classification.confidence_per_mille < self._min_confidence:
            return _live(
                RouteReason.LOW_CONFIDENCE,
                f"{classification.confidence_per_mille} below {self._min_confidence}",
            )
        if not precondition_holds(category, facts):
            return _live(
                RouteReason.PRECONDITION_CONTRADICTION,
                f"the structured fields do not support {category.value}",
            )

        plan = self._plans.active_for(self._domain, category)
        if plan is None or plan.status is not PlanStatus.ACTIVE:
            return _live(RouteReason.NO_ACTIVE_PLAN, f"nothing active for {category.value}")
        return Route(
            kind=RouteKind.COMPILED_PLAN,
            reason=RouteReason.PLAN_MATCHED,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            detail=category.value,
        )


def _live(reason: RouteReason, detail: str) -> Route:
    return Route(kind=RouteKind.LIVE_AGENT, reason=reason, detail=detail)
