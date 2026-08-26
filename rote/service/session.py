from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from rote.agent.models.language import LLM_MODE, ROTE_CLASSIFIER, classifier_from_env
from rote.bootstrap.evidence_corruption import EvidenceError, corrupt
from rote.bootstrap.system import CompiledSystem
from rote.contracts.canonical import canonical_hash
from rote.contracts.classifier import Classification, ClassifierModel
from rote.contracts.common import (
    GENERATED_CATEGORIES,
    Currency,
    Domain,
    ExceptionCategory,
)
from rote.contracts.errors import ClassifierError
from rote.contracts.execution import ExecutionOutcome, ExecutionResult, ResultVerdict
from rote.contracts.ledger import LedgerEventType
from rote.contracts.plan import Plan, PlanStep
from rote.contracts.policy import ExecutionPath, PolicyConfig, PolicyContext
from rote.contracts.reconciliation import (
    GeneratedDataset,
    ReconciliationException,
    ReconciliationFacts,
)
from rote.contracts.routing import PlanSource, Route, RouteKind, RouteReason
from rote.contracts.tools import Toolbox, ToolSpec
from rote.domain.generators.divergence import DivergenceLabel, inject
from rote.domain.tools.adapters import ReconciliationTools
from rote.observability.logging import get_logger
from rote.runtime.classifier import Classifier
from rote.runtime.classifier_rules import StructuredFieldsClassifier
from rote.runtime.evidence_check import VerificationOutcome, VerificationResult, verify
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

_logger = get_logger("rote.service.session")


# display text only: these DESCRIBE the predicates in rote.runtime.preconditions, they never
# implement them. A test asserts every generated category has one.
PRECONDITION_DESCRIPTIONS: dict[ExceptionCategory, str] = {
    ExceptionCategory.TIMING_CUTOFF: "amounts equal and the bank posted later than capture",
    ExceptionCategory.FEE_MISMATCH: "same currency and the bank paid less than expected",
    ExceptionCategory.PARTIAL_PAYMENT: "same currency and the bank paid less than expected",
    ExceptionCategory.FX_ROUNDING: "the bank credited a different currency",
    ExceptionCategory.TRANSPOSED_REFERENCE: (
        "amounts equal and the two references are rearrangements of each other"
    ),
    ExceptionCategory.DUPLICATE_ENTRY: "two or more candidate lines share the same reference",
}


class ProcedureFit(BaseModel):
    model_config = FROZEN

    category: str
    precondition: str
    holds: bool


# What Rote can say about a case before any model is asked. The ambiguity rule reads the
# structured evidence alone, so the queue can show its judgement for free -- which matters when
# the classifier is a metered hosted model, and reads more honestly either way.
class QueueTriage(BaseModel):
    model_config = FROZEN

    exception_id: str
    fitting_categories: tuple[str, ...]
    ambiguous: bool
    eligible: bool
    status: str


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
    # how many untrusted blocks were kept away from the classifier, so withholding is
    # something the runtime reports rather than something a reader has to infer
    untrusted_withheld: int = 0


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
    verification: VerificationResult | None
    procedures: tuple[ProcedureFit, ...]
    fitting_categories: tuple[str, ...]
    corrupted_with: str | None


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
    untrusted_withheld: int = 0


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
        classifier_model: ClassifierModel | None = None,
        verify_evidence: bool = False,
    ) -> None:
        # off by default so the frozen V2 path stays exactly reproducible
        self.verifies_evidence = verify_evidence
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
        # the upstream model is swappable so a different classifier can be measured against the
        # same runtime; Rote treats every answer identically whatever produced it
        self._model: ClassifierModel = classifier_model or StructuredFieldsClassifier()
        self._classifier = Classifier(model=self._model)
        self._resolved: dict[str, ResolutionView] = {}
        self._original: dict[str, ReconciliationFacts] = {}
        self._corrupted: dict[str, str] = {}
        self._truth_for_demo = {t.exception_id: t.category for t in dataset.ground_truths}
        # keyed by the hash of the evidence, so altering a case re-asks and nothing has to
        # remember to invalidate it. A hosted model is metered, and asking it the same
        # question twice buys nothing at temperature zero.
        self._classified: dict[str, tuple[str, Classification]] = {}

    @property
    def classifier_model_id(self) -> str:
        return self._model.model_id

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
        facts = exception.facts.model_dump(mode="json")
        fitting = _fitting(facts)
        return InvestigationDetail(
            exception_id=exception.exception_id,
            domain=exception.domain,
            facts=facts,
            untrusted=tuple(
                UntrustedBlockView(
                    source_path=block.source_path,
                    content=block.content,
                    byte_length=block.byte_length,
                )
                for block in exception.untrusted
            ),
            verification=verify(exception.facts, self._verification_boundary(exception_id)),
            procedures=tuple(
                ProcedureFit(
                    category=member.value,
                    precondition=PRECONDITION_DESCRIPTIONS[member],
                    holds=member.value in fitting,
                )
                for member in GENERATED_CATEGORIES
            ),
            fitting_categories=fitting,
            corrupted_with=self._corrupted.get(exception_id),
        )

    # demonstration control: rewrites the EVIDENCE for one synthetic case. The world is never
    # touched, so the authoritative record still holds the truth and the disagreement is real.
    def corrupt_case(self, exception_id: str, error: EvidenceError) -> InvestigationDetail:
        exception = self._exception(exception_id)
        self._original.setdefault(exception_id, exception.facts)
        # the true category is used ONLY to fabricate a convincing wrong-looking case; it is
        # never handed to the classifier, router or verifier, which see the corrupted facts alone
        corrupted, applied = corrupt(self._original[exception_id], error, self._truth_for_demo)
        if applied:
            self._replace_facts(exception_id, corrupted)
            self._corrupted[exception_id] = error.value
        return self.investigation(exception_id)

    def restore_case(self, exception_id: str) -> InvestigationDetail:
        self._exception(exception_id)
        original = self._original.pop(exception_id, None)
        if original is not None:
            self._replace_facts(exception_id, original)
        self._corrupted.pop(exception_id, None)
        return self.investigation(exception_id)

    def corrupted_cases(self) -> dict[str, str]:
        return dict(self._corrupted)

    def count_financial_intents(self, task_id: str) -> int:
        return self.count_events(task_id, LedgerEventType.INTENT)

    # a decision made on different evidence is a different decision, so the cached one is dropped
    def _replace_facts(self, exception_id: str, facts: ReconciliationFacts) -> None:
        was = self._exceptions[exception_id]
        self._exceptions[exception_id] = ReconciliationException(
            exception_id=was.exception_id,
            domain=was.domain,
            facts=facts,
            untrusted=was.untrusted,
        )
        self._resolved.pop(exception_id, None)

    # model-free: the evidence decides this, so listing a backlog costs nothing
    def triage(self, exception_id: str) -> QueueTriage:
        exception = self._exception(exception_id)
        fitting = _fitting(exception.facts.model_dump(mode="json"))
        return QueueTriage(
            exception_id=exception_id,
            fitting_categories=fitting,
            ambiguous=len(fitting) > 1,
            eligible=len(fitting) == 1,
            status=self._status(exception_id),
        )

    # routing only: no plan is executed, no tool is called, nothing is written
    def preview(self, exception_id: str) -> RoutingPreview:
        exception = self._exception(exception_id)
        facts = exception.facts.model_dump(mode="json")
        classification, route, _spy, withheld = self._route(exception, facts)
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
            untrusted_withheld=withheld,
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

        # the evidence is checked against the authoritative record BEFORE the router runs, so a
        # refusal here never reaches a plan lookup. Every read goes through the same gate.
        if self.verifies_evidence:
            checked = verify(exception.facts, self._verification_boundary(exception_id))
            if checked.outcome is not VerificationOutcome.AGREEMENT:
                view = self._evidence_refusal(
                    exception_id, checked, facts, before_hash, before_entries
                )
                self._resolved[exception_id] = view
                return view

        classification, route, spy, withheld = self._route(exception, facts)
        fitting = _fitting(facts)

        if route.kind is not RouteKind.COMPILED_PLAN or route.plan_id is None:
            view = self._refusal(
                exception_id, classification, route, fitting, spy, before_hash, before_entries
            ).model_copy(update={"untrusted_withheld": withheld})
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
        ).model_copy(update={"untrusted_withheld": withheld})
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
    ) -> tuple[Any, Route, _SpyPlanSource, int]:
        spy = _SpyPlanSource(self.registry)
        # D5: merchant free text may reach a local model only. A hosted model is handed the
        # structured evidence alone, and the count of what was kept back is reported.
        untrusted = exception.untrusted if self._model.is_local else ()
        withheld = len(exception.untrusted) - len(untrusted)
        fingerprint = canonical_hash(facts)
        remembered = self._classified.get(exception.exception_id)
        if remembered is not None and remembered[0] == fingerprint:
            classification = remembered[1]
        else:
            try:
                classification = self._classifier.classify(facts, untrusted, exception.exception_id)
            except Exception as error:
                # deliberately not remembered: a timeout is not a verdict on this case
                return (*self._classifier_down(exception.exception_id, error), spy, withheld)
            self._classified[exception.exception_id] = (fingerprint, classification)
        router = Router(
            plans=spy,
            domain=Domain.RECONCILIATION,
            min_confidence_per_mille=DEFAULT_MIN_CONFIDENCE_PER_MILLE,
        )
        return classification, router.route(facts, classification), spy, withheld

    # a provider outage is a refusal, not a crash and not a quiet change of classifier: the
    # router is never reached, so no plan can be looked up on an answer nobody gave
    def _classifier_down(self, exception_id: str, error: Exception) -> tuple[Classification, Route]:
        detail = f"the classifier could not be reached: {type(error).__name__}: {error}"[:200]
        _logger.info(
            "classifier_unavailable",
            correlation_id=exception_id,
            model_id=self._model.model_id,
            error=type(error).__name__,
        )
        return (
            Classification(
                category=ExceptionCategory.UNKNOWN,
                confidence_per_mille=0,
                model_id=self._model.model_id,
                prompt_template_id=self._model.prompt_template_id,
            ),
            Route(
                kind=RouteKind.LIVE_AGENT,
                reason=RouteReason.CLASSIFIER_UNAVAILABLE,
                detail=detail,
            ),
        )

    # a read-only, category-free boundary: the gate's category=None rule allows exactly the
    # read tools, and no write tool is reachable from here
    def _verification_boundary(self, exception_id: str) -> Toolbox:
        return self.gate.for_task(
            PolicyContext(
                task_id=exception_id,
                correlation_id=f"{exception_id}:verification",
                path=self.execution_path,
                category=None,
                actor="system:verifier",
            )
        )

    def _evidence_refusal(
        self,
        exception_id: str,
        checked: VerificationResult,
        facts: dict[str, Any],
        before_hash: str,
        before_entries: int,
    ) -> ResolutionView:
        mismatched = checked.outcome is VerificationOutcome.MISMATCH
        reason = RouteReason.EVIDENCE_MISMATCH if mismatched else RouteReason.EVIDENCE_UNVERIFIABLE
        fields = checked.mismatched_fields if mismatched else checked.unverifiable_fields
        detail = ", ".join(fields)
        headline = (
            f"The evidence disagrees with the authoritative record: {detail}."
            if mismatched
            else f"The evidence could not be confirmed against the record: {detail}."
        )
        after = self.world_view().world_hash
        return ResolutionView(
            exception_id=exception_id,
            decision=Decision.ESCALATE,
            headline=headline,
            already_resolved=False,
            classified_as="",
            classifier_confidence_per_mille=0,
            fitting_categories=_fitting(facts),
            co_holding_categories=(),
            route_kind=RouteKind.LIVE_AGENT,
            route_reason=reason,
            route_detail=detail,
            plan_id=None,
            plan_version=None,
            plan_lookups=0,
            compiled_steps_executed=0,
            calls=(),
            guard_inspections=0,
            guard_objection="",
            model_calls_classification=0,
            model_calls_after_classification=0,
            outcome="handed to the live agent",
            outcome_hash="",
            handover_reason=reason.value,
            handover_step=None,
            quarantined_result=None,
            world_hash_before=before_hash,
            world_hash_after=after,
            world_changed=before_hash != after,
            ledger_before=before_entries,
            ledger_after=len(self.ledger.entries),
            ledger_valid=bool(self.ledger.verify().valid),
        )

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


# the flags are the cache key, so the model is rebuilt from the environment on a reset rather
# than being carried over from a configuration that may since have changed
@functools.lru_cache(maxsize=4)
def live_session(verify_evidence: bool = False, use_llm: bool = False) -> SessionRuntime:
    return SessionRuntime(
        system=compiled_system(),
        dataset=demo_dataset(),
        classifier_model=configured_classifier() if use_llm else None,
        verify_evidence=verify_evidence,
    )


# raises rather than returning the deterministic classifier: asking for a model that cannot be
# built is a deployment error, and hiding it behind a working default would make it invisible
def configured_classifier() -> ClassifierModel:
    model = classifier_from_env(os.environ)
    if model is None:
        raise ClassifierError(
            f"{ROTE_CLASSIFIER} does not select a real model, so none can be built"
        )
    return model


# a fresh world, gate, ledger and resolved-case cache for the next rehearsal. The compiled
# system and the dataset are reused, so this costs no recompilation and grants no authority.
def reset_session(verify_evidence: bool = False, use_llm: bool = False) -> SessionRuntime:
    live_session.cache_clear()
    return live_session(verify_evidence, use_llm)


__all__ = [
    "LLM_MODE",
    "QueueTriage",
    "ROTE_CLASSIFIER",
    "BacklogItem",
    "InvestigationDetail",
    "LedgerView",
    "ResolutionView",
    "RoutingPreview",
    "SessionRuntime",
    "WorldView",
    "configured_classifier",
    "live_session",
    "reset_session",
]
