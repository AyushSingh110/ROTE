from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from rote.bootstrap.system import CompiledSystem
from rote.contracts.canonical import canonical_hash
from rote.contracts.common import (
    GENERATED_CATEGORIES,
    Currency,
    Domain,
    ExceptionCategory,
)
from rote.contracts.execution import ExecutionOutcome, ExecutionResult, ResultVerdict
from rote.contracts.ledger import LedgerEventType
from rote.contracts.plan import Plan, PlanStep
from rote.contracts.policy import ExecutionPath, PolicyConfig, PolicyContext
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.routing import PlanSource, Route, RouteKind, RouteReason
from rote.contracts.tools import ToolSpec
from rote.domain.generators.divergence import DivergenceLabel, inject
from rote.domain.tools.adapters import ReconciliationTools
from rote.runtime.classifier import Classifier
from rote.runtime.classifier_rules import StructuredFieldsClassifier
from rote.runtime.executor import execute_plan
from rote.runtime.guard import Guard, default_guard_config
from rote.runtime.preconditions import precondition_holds
from rote.runtime.router import DEFAULT_MIN_CONFIDENCE_PER_MILLE, Router
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import default_policy_config
from rote.service.scenario import (
    Decision,
    UntrustedBlockView,
    compiled_system,
    demo_dataset,
)

FROZEN = ConfigDict(extra="forbid", frozen=True)
COUNTS_MODEL_CALLS = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class BacklogItem(BaseModel):
    model_config = FROZEN

    exception_id: str
    internal_minor_units: int
    internal_currency: str
    bank_minor_units: int | None
    bank_currency: str | None
    captured_on: str
    candidate_lines: int
    has_untrusted_note: bool
    status: str


class RoutingPreview(BaseModel):
    model_config = COUNTS_MODEL_CALLS

    exception_id: str
    classified_as: str
    classifier_confidence_per_mille: int
    fitting_categories: tuple[str, ...]
    co_holding_categories: tuple[str, ...]
    ambiguous: bool
    route_kind: RouteKind
    route_reason: RouteReason
    route_detail: str
    plan_id: str | None
    plan_version: int | None


class CallView(BaseModel):
    model_config = FROZEN

    tool: str
    args: dict[str, Any]


class InvestigationDetail(BaseModel):
    model_config = FROZEN

    exception_id: str
    domain: Domain
    facts: dict[str, Any]
    untrusted: tuple[UntrustedBlockView, ...]


class ResolutionView(BaseModel):
    model_config = COUNTS_MODEL_CALLS

    exception_id: str
    decision: Decision
    headline: str
    already_resolved: bool
    classified_as: str
    classifier_confidence_per_mille: int
    fitting_categories: tuple[str, ...]
    co_holding_categories: tuple[str, ...]
    route_kind: RouteKind
    route_reason: RouteReason
    route_detail: str
    plan_id: str | None
    plan_version: int | None
    plan_lookups: int
    compiled_steps_executed: int
    calls: tuple[CallView, ...]
    guard_inspections: int
    guard_objection: str
    model_calls_classification: int
    model_calls_after_classification: int
    outcome: str
    outcome_hash: str
    handover_reason: str
    handover_step: int | None
    quarantined_result: dict[str, Any] | None
    world_hash_before: str
    world_hash_after: str
    world_changed: bool
    ledger_before: int
    ledger_after: int
    ledger_valid: bool


class LedgerEntryView(BaseModel):
    model_config = FROZEN

    seq: int
    event_type: str
    task_id: str
    actor: str
    tool: str
    detail: str
    dry_run: bool
    occurred_at: str


class LedgerView(BaseModel):
    model_config = FROZEN

    total: int
    valid: bool
    first_broken_seq: int | None
    entries: tuple[LedgerEntryView, ...]


class WorldView(BaseModel):
    model_config = FROZEN

    world_hash: str
    settlement_records: int
    matched_records: int
    partially_settled_records: int
    adjustments: int
    voided_lines: int


class _SpyPlanSource:
    def __init__(self, inner: PlanSource) -> None:
        self._inner = inner
        self.lookups = 0

    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        self.lookups += 1
        return self._inner.active_for(domain, category)


class _RejectEverything:
    def check_proposed_action(
        self, step: PlanStep, arguments: dict[str, Any], task_input: dict[str, Any]
    ) -> ResultVerdict:
        del arguments, task_input
        return ResultVerdict(passed=False, reason=f"rejecting {step.tool} for the demonstration")

    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> ResultVerdict:
        del step, result, attempts
        return ResultVerdict(passed=True)


# adds a field to one step's result, using the existing divergence generator
class _DriftingToolbox:
    enforces_policy = True
    mutates_the_world = True

    def __init__(self, inner: Any, drift_on_step: int) -> None:
        self._inner = inner
        self._drift_on = drift_on_step
        self._calls = 0

    def available_tools(self) -> tuple[ToolSpec, ...]:
        specs: tuple[ToolSpec, ...] = self._inner.available_tools()
        return specs

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._inner.invoke(name, payload)
        self._calls += 1
        if self._calls - 1 != self._drift_on:
            return result
        case = inject(DivergenceLabel.SCHEMA_DRIFT_ADDED, result, seed=7)
        return case.result if case.applied else result


# one world, one gate and one ledger for the whole process: idempotency and the rolling spend
# window are gate state, and a per-request gate would silently reset both
class SessionRuntime:
    execution_path = ExecutionPath.COMPILED_PLAN

    def __init__(
        self,
        *,
        system: CompiledSystem,
        dataset: GeneratedDataset,
        policy: PolicyConfig | None = None,
    ) -> None:
        self._dataset = dataset
        self._exceptions = {e.exception_id: e for e in dataset.exceptions}
        self._adapters = ReconciliationTools.from_snapshot(dataset.world)
        self.ledger = Ledger()
        self.gate = PolicyGate(
            adapters=self._adapters,
            config=policy or default_policy_config(),
            ledger=self.ledger,
            clock=_clock(),
        )
        self.registry = system.registry
        self._model = StructuredFieldsClassifier()
        self._classifier = Classifier(model=self._model)
        self._resolved: dict[str, ResolutionView] = {}

    @property
    def classifier_is_local(self) -> bool:
        return bool(self._model.is_local)

    def backlog(self) -> tuple[BacklogItem, ...]:
        return tuple(
            BacklogItem(
                exception_id=exception.exception_id,
                internal_minor_units=exception.facts.internal_amount.minor_units,
                internal_currency=exception.facts.internal_amount.currency.value,
                bank_minor_units=(
                    None
                    if exception.facts.bank_amount is None
                    else exception.facts.bank_amount.minor_units
                ),
                bank_currency=(
                    None
                    if exception.facts.bank_amount is None
                    else exception.facts.bank_amount.currency.value
                ),
                captured_on=exception.facts.captured_on.isoformat(),
                candidate_lines=len(exception.facts.candidate_bank_line_ids),
                has_untrusted_note=bool(exception.untrusted),
                status=self._status(exception.exception_id),
            )
            for exception in self._dataset.exceptions
        )

    def investigation(self, exception_id: str) -> InvestigationDetail:
        exception = self._exception(exception_id)
        return InvestigationDetail(
            exception_id=exception.exception_id,
            domain=exception.domain,
            facts=exception.facts.model_dump(mode="json"),
            untrusted=tuple(
                UntrustedBlockView(
                    source_path=block.source_path,
                    content=block.content,
                    byte_length=block.byte_length,
                )
                for block in exception.untrusted
            ),
        )

    # routing only: no plan is executed, no tool is called, nothing is written
    def preview(self, exception_id: str) -> RoutingPreview:
        exception = self._exception(exception_id)
        facts = exception.facts.model_dump(mode="json")
        classification, route, _spy = self._route(exception, facts)
        fitting = _fitting(facts)
        return RoutingPreview(
            exception_id=exception_id,
            classified_as=classification.category.value,
            classifier_confidence_per_mille=classification.confidence_per_mille,
            fitting_categories=fitting,
            co_holding_categories=fitting if len(fitting) > 1 else (),
            ambiguous=len(fitting) > 1,
            route_kind=route.kind,
            route_reason=route.reason,
            route_detail=route.detail,
            plan_id=route.plan_id,
            plan_version=route.plan_version,
        )

    def resolve(
        self,
        exception_id: str,
        *,
        reject_everything: bool = False,
        drift_on_step: int | None = None,
    ) -> ResolutionView:
        exception = self._exception(exception_id)
        if exception_id in self._resolved:
            return self._resolved[exception_id].model_copy(
                update={"already_resolved": True, "world_changed": False}
            )

        facts = exception.facts.model_dump(mode="json")
        before_hash = self.world_view().world_hash
        before_entries = len(self.ledger.entries)
        classification, route, spy = self._route(exception, facts)
        fitting = _fitting(facts)

        if route.kind is not RouteKind.COMPILED_PLAN or route.plan_id is None:
            view = self._refusal(
                exception_id, classification, route, fitting, spy, before_hash, before_entries
            )
            self._resolved[exception_id] = view
            return view

        plan = self.registry.get(route.plan_id, route.plan_version or 1)
        guard = Guard(config=default_guard_config())
        toolbox: Any = self.gate.for_task(
            PolicyContext(
                task_id=exception_id,
                correlation_id=f"{exception_id}:live",
                path=self.execution_path,
                category=plan.category,
                actor="system:executor",
            )
        )
        if drift_on_step is not None:
            toolbox = _DriftingToolbox(toolbox, drift_on_step=drift_on_step)

        result = execute_plan(
            plan=plan,
            task_input=facts,
            toolbox=toolbox,
            inspector=_RejectEverything() if reject_everything else guard,
        )
        view = self._executed(
            exception_id,
            classification,
            route,
            fitting,
            spy,
            plan,
            result,
            guard,
            before_hash,
            before_entries,
        )
        self._resolved[exception_id] = view
        return view

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return self._adapters.available_tools()

    # the same gated boundary the compiled plan used, so a caller can replay a committed call
    # straight at the gate. It grants no new authority: every call still passes the full gate.
    def boundary_for(self, exception_id: str) -> Any:
        exception = self._exception(exception_id)
        view = self._resolved.get(exception_id)
        category = None
        if view is not None and view.plan_id is not None:
            category = self.registry.get(view.plan_id, view.plan_version or 1).category
        return self.gate.for_task(
            PolicyContext(
                task_id=exception.exception_id,
                correlation_id=f"{exception.exception_id}:live",
                path=self.execution_path,
                category=category,
                actor="system:executor",
            )
        )

    def resolution_for(self, exception_id: str) -> ResolutionView | None:
        return self._resolved.get(exception_id)

    def ledger_view(self, limit: int = 200) -> LedgerView:
        verification = self.ledger.verify()
        entries = self.ledger.entries[-limit:]
        return LedgerView(
            total=len(self.ledger.entries),
            valid=bool(verification.valid),
            first_broken_seq=verification.first_broken_seq,
            entries=tuple(
                LedgerEntryView(
                    seq=entry.seq,
                    event_type=entry.event_type.value,
                    task_id=entry.task_id,
                    actor=entry.actor,
                    tool=str(entry.payload.get("tool", "")),
                    detail=str(entry.payload.get("reason", entry.payload.get("key", ""))),
                    dry_run=entry.dry_run,
                    occurred_at=entry.occurred_at.isoformat(),
                )
                for entry in entries
            ),
        )

    def world_view(self) -> WorldView:
        snapshot = self._adapters.snapshot()
        statuses = [record.status.value for record in snapshot.settlement_records]
        return WorldView(
            world_hash=canonical_hash(snapshot.model_dump(mode="json")),
            settlement_records=len(snapshot.settlement_records),
            matched_records=sum(1 for status in statuses if status == "matched"),
            partially_settled_records=sum(1 for s in statuses if s == "partially_settled"),
            adjustments=len(snapshot.adjustments),
            voided_lines=sum(1 for line in snapshot.bank_lines if line.voided),
        )

    def count_events(self, task_id: str, event_type: LedgerEventType) -> int:
        return sum(
            1
            for entry in self.ledger.entries
            if entry.task_id == task_id and entry.event_type is event_type
        )

    def window_spend(self, currency: Currency) -> int:
        snapshot = self._adapters.snapshot()
        return sum(
            abs(adjustment.amount.minor_units)
            for adjustment in snapshot.adjustments
            if adjustment.amount.currency is currency
        )

    def injected_note_case(self) -> str:
        for exception in self._dataset.exceptions:
            carries = any("</merchant_note>" in b.content for b in exception.untrusted)
            if carries and len(_fitting(exception.facts.model_dump(mode="json"))) == 1:
                return exception.exception_id
        raise LookupError("no unambiguous exception carrying an injected note")

    def _exception(self, exception_id: str) -> ReconciliationException:
        exception = self._exceptions.get(exception_id)
        if exception is None:
            raise LookupError(f"no exception {exception_id!r} in this backlog")
        return exception

    def _status(self, exception_id: str) -> str:
        view = self._resolved.get(exception_id)
        if view is None:
            return "open"
        return "automated" if view.decision is Decision.AUTOMATE else "refused"

    def _route(
        self, exception: ReconciliationException, facts: dict[str, Any]
    ) -> tuple[Any, Route, _SpyPlanSource]:
        classification = self._classifier.classify(
            facts, exception.untrusted, exception.exception_id
        )
        spy = _SpyPlanSource(self.registry)
        router = Router(
            plans=spy,
            domain=Domain.RECONCILIATION,
            min_confidence_per_mille=DEFAULT_MIN_CONFIDENCE_PER_MILLE,
        )
        return classification, router.route(facts, classification), spy

    def _refusal(
        self,
        exception_id: str,
        classification: Any,
        route: Route,
        fitting: tuple[str, ...],
        spy: _SpyPlanSource,
        before_hash: str,
        before_entries: int,
    ) -> ResolutionView:
        co_holding = fitting if route.reason is RouteReason.AMBIGUOUS_EVIDENCE else ()
        headline = (
            f"Multiple procedures are consistent with this evidence: {', '.join(co_holding)}."
            if co_holding
            else f"No compiled procedure was served: {route.detail or route.reason.value}."
        )
        after = self.world_view().world_hash
        return ResolutionView(
            exception_id=exception_id,
            decision=Decision.ESCALATE,
            headline=headline,
            already_resolved=False,
            classified_as=classification.category.value,
            classifier_confidence_per_mille=classification.confidence_per_mille,
            fitting_categories=fitting,
            co_holding_categories=co_holding,
            route_kind=route.kind,
            route_reason=route.reason,
            route_detail=route.detail,
            plan_id=None,
            plan_version=None,
            plan_lookups=spy.lookups,
            compiled_steps_executed=0,
            calls=(),
            guard_inspections=0,
            guard_objection="",
            model_calls_classification=1,
            model_calls_after_classification=0,
            outcome="handed to the live agent",
            outcome_hash="",
            handover_reason=route.reason.value,
            handover_step=None,
            quarantined_result=None,
            world_hash_before=before_hash,
            world_hash_after=after,
            world_changed=before_hash != after,
            ledger_before=before_entries,
            ledger_after=len(self.ledger.entries),
            ledger_valid=bool(self.ledger.verify().valid),
        )

    def _executed(
        self,
        exception_id: str,
        classification: Any,
        route: Route,
        fitting: tuple[str, ...],
        spy: _SpyPlanSource,
        plan: Plan,
        result: ExecutionResult,
        guard: Guard,
        before_hash: str,
        before_entries: int,
    ) -> ResolutionView:
        resolved = result.outcome is ExecutionOutcome.RESOLVED
        reason = "" if result.escalation_reason is None else result.escalation_reason.value
        objection = next(
            (v.reason for v in guard.inspections if not v.passed and v.reason),
            "",
        )
        after = self.world_view().world_hash
        headline = (
            f"Exactly one procedure fits: {plan.category.value}. Executed deterministically."
            if resolved
            else _stopped_headline(reason, objection)
        )
        return ResolutionView(
            exception_id=exception_id,
            decision=Decision.AUTOMATE if resolved else Decision.ESCALATE,
            headline=headline,
            already_resolved=False,
            classified_as=classification.category.value,
            classifier_confidence_per_mille=classification.confidence_per_mille,
            fitting_categories=fitting,
            co_holding_categories=(),
            route_kind=route.kind,
            route_reason=route.reason,
            route_detail=route.detail,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            plan_lookups=spy.lookups,
            compiled_steps_executed=result.steps_completed,
            calls=tuple(CallView(tool=call.tool, args=dict(call.args)) for call in result.calls),
            guard_inspections=len(guard.inspections),
            guard_objection=objection,
            model_calls_classification=1,
            model_calls_after_classification=0,
            outcome=result.outcome.value,
            outcome_hash=result.outcome_hash,
            handover_reason=reason,
            handover_step=None if result.handover is None else result.handover.step_index,
            quarantined_result=None
            if result.handover is None
            else result.handover.untrusted_result,
            world_hash_before=before_hash,
            world_hash_after=after,
            world_changed=before_hash != after,
            ledger_before=before_entries,
            ledger_after=len(self.ledger.entries),
            ledger_valid=bool(self.ledger.verify().valid),
        )


def _stopped_headline(reason: str, objection: str) -> str:
    if reason == "gate_cap_exceeded":
        return "The policy gate refused: the amount exceeds its per-action cap."
    if reason == "gate_not_allowlisted":
        return "The policy gate refused: that tool is not allowlisted for this category."
    if objection:
        return f"The guard rejected the result before it became state: {objection}."
    return f"The compiled run stopped and handed over: {reason or 'no reason recorded'}."


def _fitting(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(member.value for member in GENERATED_CATEGORIES if precondition_holds(member, facts))
    )


def _clock() -> Callable[[], datetime]:
    moment = datetime(2026, 8, 23, 10, 0, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(seconds=1)
        return moment

    return tick


@functools.lru_cache(maxsize=1)
def live_session() -> SessionRuntime:
    return SessionRuntime(system=compiled_system(), dataset=demo_dataset())


__all__ = [
    "BacklogItem",
    "InvestigationDetail",
    "LedgerView",
    "ResolutionView",
    "RoutingPreview",
    "SessionRuntime",
    "WorldView",
    "live_session",
]
