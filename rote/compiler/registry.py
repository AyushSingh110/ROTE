from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import RegistryError
from rote.contracts.ledger import LedgerEntry, LedgerEvent, LedgerEventType
from rote.contracts.plan import Plan, PlanStatus
from rote.safety.ledger import Ledger

HUMAN_ACTOR_PREFIX = "human:"

TRANSITION_EVENTS: dict[PlanStatus, LedgerEventType] = {
    PlanStatus.SHADOW: LedgerEventType.PLAN_SHADOWED,
    PlanStatus.ACTIVE: LedgerEventType.PLAN_ACTIVATED,
    PlanStatus.INACTIVE: LedgerEventType.PLAN_DEACTIVATED,
    PlanStatus.RETIRED: LedgerEventType.PLAN_RETIRED,
}
EVENT_TRANSITIONS = {event: status for status, event in TRANSITION_EVENTS.items()}


class RegistryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_shadow_agreements: int = Field(gt=0)
    max_shadow_disagreements: int = Field(ge=0)


class LifecycleTransition(NamedTuple):
    seq: int
    plan_id: str
    version: int
    to_status: PlanStatus
    actor: str
    reason: str
    occurred_at: datetime


class _ShadowTally(NamedTuple):
    agreed: int
    disagreed: int


class PlanRegistry:
    def __init__(
        self, *, ledger: Ledger, clock: Callable[[], datetime], policy: RegistryPolicy
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._policy = policy
        self._plans: dict[tuple[str, int], Plan] = {}
        self._shadow: dict[tuple[str, int], _ShadowTally] = {}

    def register(self, plan: Plan, *, actor: str) -> Plan:
        key = (plan.plan_id, plan.version)
        if key in self._plans:
            raise RegistryError(f"{plan.plan_id} v{plan.version} is already registered")
        if plan.validation is None:
            raise RegistryError(f"{plan.plan_id} v{plan.version} has never been replay-validated")

        self._append(
            plan,
            LedgerEventType.PLAN_VALIDATED,
            actor,
            {
                "passed": plan.validation.passed,
                "holdout_size": plan.validation.holdout_size,
                "path_equal": plan.validation.path_equal,
                "playback_misses": plan.validation.playback_misses,
            },
        )
        target = PlanStatus.SHADOW if plan.validation.passed else PlanStatus.INACTIVE
        reason = (
            "replay validation passed" if plan.validation.passed else "replay validation failed"
        )
        stored = plan.model_copy(update={"status": target})
        self._plans[key] = stored
        self._shadow[key] = _ShadowTally(0, 0)
        return self._transition(stored, target, actor, reason)

    def observe_shadow(self, plan_id: str, version: int, *, agreed: bool, actor: str) -> None:
        plan = self.get(plan_id, version)
        if plan.status is not PlanStatus.SHADOW:
            raise RegistryError(f"{plan_id} v{version} is {plan.status.value}, not shadowing")
        tally = self._shadow[(plan_id, version)]
        tally = _ShadowTally(tally.agreed + int(agreed), tally.disagreed + int(not agreed))
        self._shadow[(plan_id, version)] = tally
        if tally.disagreed > self._policy.max_shadow_disagreements:
            self._transition(
                plan, PlanStatus.INACTIVE, actor, f"{tally.disagreed} shadow disagreements"
            )

    # no override parameter exists on purpose: there is no way to skip the evidence
    def activate(self, plan_id: str, version: int, *, actor: str, note: str) -> Plan:
        plan = self.get(plan_id, version)
        if plan.status is not PlanStatus.SHADOW:
            raise RegistryError(f"only a shadowing plan may be activated, not {plan.status.value}")
        if plan.validation is None or not plan.validation.passed:
            raise RegistryError(f"{plan_id} v{version} has no passing validation report")
        if not actor.startswith(HUMAN_ACTOR_PREFIX):
            raise RegistryError(f"activation needs a named human actor, got {actor!r}")
        if not note.strip():
            raise RegistryError("activation needs a sign-off note on the diff")

        tally = self._shadow[(plan_id, version)]
        if tally.agreed < self._policy.min_shadow_agreements:
            raise RegistryError(
                f"{tally.agreed} agreeing shadow runs, {self._policy.min_shadow_agreements} needed"
            )
        if tally.disagreed > self._policy.max_shadow_disagreements:
            raise RegistryError(f"{tally.disagreed} shadow disagreements is too many")

        self._retire_incumbent(plan, actor)
        moment = self._clock()
        promoted = plan.model_copy(
            update={"status": PlanStatus.ACTIVE, "activated_by": actor, "activated_at": moment}
        )
        self._plans[(plan_id, version)] = promoted
        return self._transition(promoted, PlanStatus.ACTIVE, actor, "signed off", note=note)

    def deactivate(self, plan_id: str, version: int, *, actor: str, reason: str) -> Plan:
        plan = self.get(plan_id, version)
        if not reason.strip():
            raise RegistryError("switching a plan off needs a reason")
        if plan.status in (PlanStatus.INACTIVE, PlanStatus.RETIRED):
            raise RegistryError(f"{plan_id} v{version} is already {plan.status.value}")
        return self._transition(plan, PlanStatus.INACTIVE, actor, reason)

    def retire(self, plan_id: str, version: int, *, actor: str, reason: str) -> Plan:
        plan = self.get(plan_id, version)
        if plan.status is not PlanStatus.ACTIVE:
            raise RegistryError(f"only an active plan may be retired, not {plan.status.value}")
        return self._transition(plan, PlanStatus.RETIRED, actor, reason)

    def get(self, plan_id: str, version: int) -> Plan:
        plan = self._plans.get((plan_id, version))
        if plan is None:
            raise RegistryError(f"no plan {plan_id} v{version} is registered")
        return plan

    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        return self._first_with(domain, category, PlanStatus.ACTIVE)

    def shadow_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        return self._first_with(domain, category, PlanStatus.SHADOW)

    def all_plans(self) -> tuple[Plan, ...]:
        return tuple(self._plans[key] for key in sorted(self._plans))

    def _first_with(
        self, domain: Domain, category: ExceptionCategory, status: PlanStatus
    ) -> Plan | None:
        for key in sorted(self._plans):
            plan = self._plans[key]
            if plan.domain is domain and plan.category is category and plan.status is status:
                return plan
        return None

    def _retire_incumbent(self, incoming: Plan, actor: str) -> None:
        incumbent = self.active_for(incoming.domain, incoming.category)
        if incumbent is not None:
            self._transition(
                incumbent,
                PlanStatus.RETIRED,
                actor,
                f"superseded by v{incoming.version}",
            )

    def _transition(
        self, plan: Plan, target: PlanStatus, actor: str, reason: str, note: str = ""
    ) -> Plan:
        moved = plan.model_copy(update={"status": target})
        self._plans[(plan.plan_id, plan.version)] = moved
        payload: dict[str, Any] = {"to_status": target.value, "reason": reason}
        if note:
            payload["note"] = note
        self._append(moved, TRANSITION_EVENTS[target], actor, payload)
        return moved

    def _append(
        self, plan: Plan, event_type: LedgerEventType, actor: str, payload: dict[str, Any]
    ) -> None:
        self._ledger.append(
            LedgerEvent(
                correlation_id=f"{plan.plan_id}:v{plan.version}",
                task_id=plan.plan_id,
                event_type=event_type,
                actor=actor,
                payload={**payload, "plan_id": plan.plan_id, "version": plan.version},
                dry_run=True,
                occurred_at=self._clock(),
            )
        )


def lifecycle_from_ledger(
    entries: Sequence[LedgerEntry], plan_id: str
) -> tuple[LifecycleTransition, ...]:
    return tuple(
        LifecycleTransition(
            seq=entry.seq,
            plan_id=str(entry.payload["plan_id"]),
            version=int(entry.payload["version"]),
            to_status=EVENT_TRANSITIONS[entry.event_type],
            actor=entry.actor,
            reason=str(entry.payload.get("reason", "")),
            occurred_at=entry.occurred_at,
        )
        for entry in entries
        if entry.event_type in EVENT_TRANSITIONS and entry.payload.get("plan_id") == plan_id
    )


__all__ = [
    "LifecycleTransition",
    "PlanRegistry",
    "RegistryPolicy",
    "lifecycle_from_ledger",
]
