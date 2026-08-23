from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rote.agent.loop import run_agent
from rote.agent.models.offline import OfflineHeuristicModel
from rote.contracts.agent import AgentBudget
from rote.contracts.canonical import canonical_hash
from rote.contracts.common import GENERATED_CATEGORIES, Domain, ExceptionCategory
from rote.contracts.execution import ExecutionOutcome, ExecutionResult
from rote.contracts.plan import Plan
from rote.contracts.policy import ExecutionPath, PolicyConfig, PolicyContext
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.routing import PlanSource, Route, RouteKind, RouteReason
from rote.contracts.tools import ToolSpec
from rote.contracts.trajectory import Trajectory
from rote.domain.generators.divergence import DivergenceLabel, inject
from rote.domain.generators.reconciliation import INJECTION_SENTENCES, generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.eval.classifier_double import StructuredFieldsClassifier
from rote.eval.harness import MIN_CONFIDENCE_PER_MILLE, CompiledSystem, compile_and_activate
from rote.recorder.recorder import TrajectoryRecorder
from rote.runtime.classifier import Classifier
from rote.runtime.executor import execute_plan
from rote.runtime.guard import Guard, default_guard_config
from rote.runtime.preconditions import precondition_holds
from rote.runtime.router import Router
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import default_policy_config

FROZEN = ConfigDict(extra="forbid", frozen=True)
# model_calls_* are counts of model invocations, not pydantic model fields
COUNTS_MODEL_CALLS = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
FIT_SEED, FIT_COUNT = 5, 1500
EVAL_SEED, EVAL_COUNT = 91, 500
# small enough that any real adjustment is over it, so the gate boundary is visible on stage
DEMO_CAP_MINOR_UNITS = 1


class ScenarioId(StrEnum):
    AUTOMATED = "automated"
    AMBIGUOUS = "ambiguous"
    INJECTED_NOTE = "injected_note"
    SCHEMA_DRIFT = "schema_drift"
    CAP_BREACH = "cap_breach"
    KILL_SWITCH = "kill_switch"


class Decision(StrEnum):
    AUTOMATE = "automate"
    ESCALATE = "escalate"


class ScenarioSpec(BaseModel):
    model_config = FROZEN

    id: ScenarioId
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    closing_line: str = Field(min_length=1)
    fit_seed: int = Field(gt=0)
    eval_seed: int = Field(gt=0)
    eval_count: int = Field(gt=0)
    exception_id: str = ""


class UntrustedBlockView(BaseModel):
    model_config = FROZEN

    source_path: str
    content: str
    byte_length: int


class ToolCallView(BaseModel):
    model_config = FROZEN

    index: int
    tool: str
    arguments: dict[str, Any]
    gate_verdict: str
    error: str | None
    result_fields: tuple[str, ...]


class InvestigationView(BaseModel):
    model_config = FROZEN

    exception_id: str
    domain: Domain
    facts: dict[str, Any]
    untrusted: tuple[UntrustedBlockView, ...]
    trajectory: tuple[ToolCallView, ...]
    agent_outcome: str
    checker_verdict: str | None


class CandidateView(BaseModel):
    model_config = FROZEN

    category: str
    precondition_holds: bool


class BindingView(BaseModel):
    model_config = FROZEN

    arg_name: str
    kind: str
    source: str
    evidence_run_count: int
    alternatives: int


class PlanStepView(BaseModel):
    model_config = FROZEN

    index: int
    tool: str
    bindings: tuple[BindingView, ...]
    known_result_shapes: int
    numeric_expectations: int
    categorical_expectations: int
    invariants: tuple[str, ...]


class PlanView(BaseModel):
    model_config = FROZEN

    plan_id: str
    version: int
    status: str
    skeleton: tuple[str, ...]
    built_from_runs: int
    coverage_count: int
    coverage_total: int
    steps: tuple[PlanStepView, ...]
    holdout_size: int
    holdout_path_equal: int
    playback_misses: int
    validation_passed: bool
    approved_by: str
    approved_at: str
    agent_model_id: str


class EvidenceView(BaseModel):
    model_config = FROZEN

    candidates: tuple[CandidateView, ...]
    fitting_categories: tuple[str, ...]
    ambiguous: bool
    classified_as: str
    classifier_confidence_per_mille: int
    plan: PlanView | None
    plan_lookups: int
    shadow_agreements_required: int
    ledger_entries: int
    ledger_valid: bool
    note: str = ""


class DecisionView(BaseModel):
    model_config = COUNTS_MODEL_CALLS

    decision: Decision
    headline: str
    route_kind: RouteKind
    route_reason: RouteReason
    route_detail: str
    co_holding_categories: tuple[str, ...]
    plan_id: str | None
    plan_version: int | None
    model_calls_classification: int
    model_calls_after_classification: int
    compiled_steps_executed: int
    outcome: str
    outcome_hash: str
    replay_match: bool | None
    replay_outcome_hash: str | None
    guard_objection: str
    handover_reason: str
    handover_step: int | None
    quarantined_result: dict[str, Any] | None
    world_hash_before: str
    world_hash_after: str
    ledger_entries: int
    ledger_valid: bool


class ScenarioResult(BaseModel):
    model_config = FROZEN

    spec: ScenarioSpec
    investigation: InvestigationView
    evidence: EvidenceView
    decision: DecisionView
    # never true in this prototype: both models are stand-ins and the world is synthetic
    research_grade: bool = False


SCENARIOS: dict[ScenarioId, ScenarioSpec] = {
    ScenarioId.AUTOMATED: ScenarioSpec(
        id=ScenarioId.AUTOMATED,
        title="Evidence is sufficient",
        question="Does exactly one procedure fit this evidence?",
        closing_line=(
            "A deterministic procedure was granted authority because the evidence "
            "uniquely supported it."
        ),
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
    ScenarioId.AMBIGUOUS: ScenarioSpec(
        id=ScenarioId.AMBIGUOUS,
        title="Evidence is insufficient",
        question="Could more than one procedure explain this evidence?",
        closing_line="Rote refused to guess.",
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
    ScenarioId.INJECTED_NOTE: ScenarioSpec(
        id=ScenarioId.INJECTED_NOTE,
        title="A merchant note tries to give instructions",
        question="Can free text change what the system does?",
        closing_line="The note stayed data. It never became an instruction.",
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
    ScenarioId.SCHEMA_DRIFT: ScenarioSpec(
        id=ScenarioId.SCHEMA_DRIFT,
        title="A tool result changes shape",
        question="What happens when a bank changes its response format?",
        closing_line="The guard rejected the result before it became state.",
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
    ScenarioId.CAP_BREACH: ScenarioSpec(
        id=ScenarioId.CAP_BREACH,
        title="The amount exceeds its cap",
        question="Who decides how much a compiled plan may move?",
        closing_line="The policy gate refused, and the compiled path could not route around it.",
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
    ScenarioId.KILL_SWITCH: ScenarioSpec(
        id=ScenarioId.KILL_SWITCH,
        title="A plan is switched off",
        question="Can a compiled plan be withdrawn immediately?",
        closing_line="A deactivated plan is no longer served, and nothing else changed.",
        fit_seed=FIT_SEED,
        eval_seed=EVAL_SEED,
        eval_count=EVAL_COUNT,
    ),
}


def scenario_spec(scenario: ScenarioId) -> ScenarioSpec:
    return SCENARIOS[scenario]


def run_scenario(scenario: ScenarioId) -> ScenarioResult:
    return _run(scenario)


class _SpyPlanSource:
    def __init__(self, inner: PlanSource) -> None:
        self._inner = inner
        self.lookups = 0

    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        self.lookups += 1
        return self._inner.active_for(domain, category)


# returns one step's result with an extra field, using the existing divergence generator
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

    def invoke(self, name: str, payload: Any) -> dict[str, Any]:
        result: dict[str, Any] = self._inner.invoke(name, payload)
        self._calls += 1
        if self._calls - 1 != self._drift_on:
            return result
        case = inject(DivergenceLabel.SCHEMA_DRIFT_ADDED, result, seed=7)
        return case.result if case.applied else result


@functools.lru_cache(maxsize=2)
def _system(killed: bool) -> CompiledSystem:
    system = compile_and_activate(fit_seed=FIT_SEED, count=FIT_COUNT)
    if killed:
        for plan in system.registry.all_plans():
            if plan.category is ExceptionCategory.FX_ROUNDING:
                system.registry.deactivate(
                    plan.plan_id,
                    plan.version,
                    actor="human:ops-lead-42",
                    reason="withdrawn for the demonstration",
                )
    return system


@functools.lru_cache(maxsize=1)
def _dataset() -> GeneratedDataset:
    return generate_dataset(seed=EVAL_SEED, count=EVAL_COUNT)


def _carries_injection(exception: ReconciliationException) -> bool:
    return any(
        sentence in block.content
        for block in exception.untrusted
        for sentence in INJECTION_SENTENCES
    )


def _fitting(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(member.value for member in GENERATED_CATEGORIES if _fits(member, facts))


def _pick(scenario: ScenarioId) -> ReconciliationException:
    data = _dataset()
    truth_of = {t.exception_id: t.category for t in data.ground_truths}
    if scenario is ScenarioId.INJECTED_NOTE:
        injected = [e for e in data.exceptions if _carries_injection(e)]
        # deliberately an UNAMBIGUOUS case: the point is that the note changed nothing, which is
        # only visible when the structured evidence still reaches a compiled plan
        for exception in injected:
            if len(_fitting(exception.facts.model_dump(mode="json"))) == 1:
                return exception
        if injected:
            return injected[0]
    wanted = (
        ExceptionCategory.FEE_MISMATCH
        if scenario is ScenarioId.AMBIGUOUS
        else ExceptionCategory.FX_ROUNDING
    )
    for exception in data.exceptions:
        if truth_of[exception.exception_id] is wanted:
            return exception
    raise LookupError(f"no exception available for {scenario.value}")


def _policy_for(scenario: ScenarioId) -> PolicyConfig:
    config = default_policy_config()
    if scenario is not ScenarioId.CAP_BREACH:
        return config
    # only the caps move; the tools, money arguments and idempotency rules are untouched
    tightened = tuple(
        rule.model_copy(
            update={"max_per_action": dict.fromkeys(rule.max_per_action, DEMO_CAP_MINOR_UNITS)}
        )
        for rule in config.rules
    )
    return config.model_copy(update={"rules": tightened})


@functools.lru_cache(maxsize=len(ScenarioId))
def _run(scenario: ScenarioId) -> ScenarioResult:
    system = _system(scenario is ScenarioId.KILL_SWITCH)
    exception = _pick(scenario)
    spec = SCENARIOS[scenario].model_copy(update={"exception_id": exception.exception_id})
    facts = exception.facts.model_dump(mode="json")

    trajectory = _recorded_trajectory(exception)
    investigation = _investigation(exception, facts, trajectory)

    classification = Classifier(model=StructuredFieldsClassifier()).classify(
        facts, exception.untrusted, exception.exception_id
    )
    spy = _SpyPlanSource(system.registry)
    router = Router(
        plans=spy, domain=Domain.RECONCILIATION, min_confidence_per_mille=MIN_CONFIDENCE_PER_MILLE
    )
    route = router.route(facts, classification)

    fitting = _fitting(facts)
    plan = (
        system.registry.get(route.plan_id, route.plan_version or 1)
        if route.plan_id is not None
        else None
    )
    evidence = _evidence(system, classification, fitting, plan, spy.lookups)
    decision = _decide(scenario, exception, facts, route, plan)
    return ScenarioResult(
        spec=spec, investigation=investigation, evidence=evidence, decision=decision
    )


def _fits(category: ExceptionCategory, facts: dict[str, Any]) -> bool:
    return precondition_holds(category, facts)


def _recorded_trajectory(exception: ReconciliationException) -> Trajectory:
    adapters = ReconciliationTools.from_snapshot(_dataset().world)
    gate = PolicyGate(
        adapters=adapters,
        config=default_policy_config(),
        ledger=Ledger(),
        clock=_clock(),
    )
    return run_agent(
        domain=exception.domain,
        task_input=exception.facts.model_dump(mode="json"),
        untrusted=exception.untrusted,
        toolbox=gate.for_task(
            PolicyContext(
                task_id=exception.exception_id,
                correlation_id=f"{exception.exception_id}:demo",
                path=ExecutionPath.LIVE_AGENT,
                category=None,
                actor="system:agent",
            )
        ),
        model=OfflineHeuristicModel(seed=1),
        recorder=TrajectoryRecorder(clock=_clock()),
        budget=AgentBudget(max_steps=12, max_tool_errors=3),
        correlation_id=f"{exception.exception_id}:demo",
    )


def _investigation(
    exception: ReconciliationException, facts: dict[str, Any], trajectory: Trajectory
) -> InvestigationView:
    return InvestigationView(
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
        trajectory=tuple(
            ToolCallView(
                index=step.index,
                tool=step.tool,
                arguments=dict(step.args),
                gate_verdict=step.gate_verdict.value,
                error=None if step.error is None else step.error.message,
                result_fields=tuple(sorted((step.result or {}).keys())),
            )
            for step in trajectory.steps
        ),
        agent_outcome=trajectory.outcome,
        checker_verdict=None,
    )


def _evidence(
    system: CompiledSystem,
    classification: Any,
    fitting: tuple[str, ...],
    plan: Plan | None,
    lookups: int,
) -> EvidenceView:
    verification = system.ledger.verify()
    return EvidenceView(
        candidates=tuple(
            CandidateView(category=member.value, precondition_holds=member.value in fitting)
            for member in sorted(GENERATED_CATEGORIES, key=lambda item: item.value)
        ),
        fitting_categories=tuple(sorted(fitting)),
        ambiguous=len(fitting) > 1,
        classified_as=classification.category.value,
        classifier_confidence_per_mille=classification.confidence_per_mille,
        plan=None if plan is None else _plan_view(plan),
        plan_lookups=lookups,
        shadow_agreements_required=20,
        ledger_entries=len(system.ledger.entries),
        ledger_valid=bool(verification.valid),
        note=(
            "Multiple procedures are consistent with this evidence."
            if len(fitting) > 1
            else "Exactly one procedure is consistent with this evidence."
        ),
    )


def _plan_view(plan: Plan) -> PlanView:
    validation = plan.validation
    return PlanView(
        plan_id=plan.plan_id,
        version=plan.version,
        status=plan.status.value,
        skeleton=plan.skeleton,
        built_from_runs=len(plan.built_from),
        coverage_count=plan.coverage_count,
        coverage_total=plan.coverage_total,
        steps=tuple(
            PlanStepView(
                index=step.index,
                tool=step.tool,
                bindings=tuple(
                    BindingView(
                        arg_name=binding.arg_name,
                        kind=binding.kind.value,
                        source=_binding_source(binding),
                        evidence_run_count=binding.evidence_run_count,
                        alternatives=len(binding.alternative_paths)
                        + len(binding.alternative_derivations),
                    )
                    for binding in step.args
                ),
                known_result_shapes=len(step.expect.result_fingerprints),
                numeric_expectations=len(step.expect.numeric_widened),
                categorical_expectations=len(step.expect.categorical_domains),
                invariants=step.expect.invariants,
            )
            for step in plan.steps
        ),
        holdout_size=0 if validation is None else validation.holdout_size,
        holdout_path_equal=0 if validation is None else validation.path_equal,
        playback_misses=0 if validation is None else validation.playback_misses,
        validation_passed=bool(validation is not None and validation.passed),
        approved_by=plan.activated_by or "",
        approved_at="" if plan.activated_at is None else plan.activated_at.isoformat(),
        agent_model_id=plan.agent_model_id,
    )


def _binding_source(binding: Any) -> str:
    if binding.json_path:
        return str(binding.json_path)
    if binding.derivation is not None:
        return f"{binding.derivation.derivation_id}(...)"
    return repr(binding.literal_value)


def _decide(
    scenario: ScenarioId,
    exception: ReconciliationException,
    facts: dict[str, Any],
    route: Route,
    plan: Plan | None,
) -> DecisionView:
    adapters = ReconciliationTools.from_snapshot(_dataset().world)
    ledger = Ledger()
    before = canonical_hash(adapters.snapshot().model_dump(mode="json"))

    if route.kind is not RouteKind.COMPILED_PLAN or plan is None:
        after = canonical_hash(adapters.snapshot().model_dump(mode="json"))
        return _refusal(scenario, route, before, after, ledger)

    gate = PolicyGate(
        adapters=adapters, config=_policy_for(scenario), ledger=ledger, clock=_clock()
    )
    toolbox: Any = gate.for_task(
        PolicyContext(
            task_id=exception.exception_id,
            correlation_id=f"{exception.exception_id}:compiled",
            path=ExecutionPath.COMPILED_PLAN,
            category=plan.category,
            actor="system:executor",
        )
    )
    if scenario is ScenarioId.SCHEMA_DRIFT:
        toolbox = _DriftingToolbox(toolbox, drift_on_step=0)

    guard = Guard(config=default_guard_config())
    result = execute_plan(plan=plan, task_input=facts, toolbox=toolbox, inspector=guard)
    after = canonical_hash(adapters.snapshot().model_dump(mode="json"))

    replay_hash: str | None = None
    if result.outcome is ExecutionOutcome.RESOLVED:
        replay_hash = _replay(exception, facts, plan)

    objection = next(
        (verdict.reason for verdict in guard.inspections if not verdict.passed and verdict.reason),
        "",
    )
    return _outcome_view(
        scenario, route, plan, result, replay_hash, objection, before, after, ledger
    )


def _replay(exception: ReconciliationException, facts: dict[str, Any], plan: Plan) -> str:
    adapters = ReconciliationTools.from_snapshot(_dataset().world)
    gate = PolicyGate(
        adapters=adapters, config=default_policy_config(), ledger=Ledger(), clock=_clock()
    )
    replayed = execute_plan(
        plan=plan,
        task_input=facts,
        toolbox=gate.for_task(
            PolicyContext(
                task_id=exception.exception_id,
                correlation_id=f"{exception.exception_id}:replay",
                path=ExecutionPath.COMPILED_PLAN,
                category=plan.category,
                actor="system:executor",
            )
        ),
        inspector=Guard(config=default_guard_config()),
    )
    return replayed.outcome_hash


def _refusal(
    scenario: ScenarioId, route: Route, before: str, after: str, ledger: Ledger
) -> DecisionView:
    co_holding = (
        tuple(part.strip() for part in route.detail.split(","))
        if route.reason is RouteReason.AMBIGUOUS_EVIDENCE
        else ()
    )
    headline = (
        f"Multiple procedures are consistent with this evidence: {route.detail}."
        if co_holding
        else f"No compiled procedure was served: {route.detail}."
    )
    return DecisionView(
        decision=Decision.ESCALATE,
        headline=headline,
        route_kind=route.kind,
        route_reason=route.reason,
        route_detail=route.detail,
        co_holding_categories=co_holding,
        plan_id=None,
        plan_version=None,
        model_calls_classification=1,
        model_calls_after_classification=0,
        compiled_steps_executed=0,
        outcome="handed to the live agent",
        outcome_hash="",
        replay_match=None,
        replay_outcome_hash=None,
        guard_objection="",
        handover_reason=route.reason.value,
        handover_step=None,
        quarantined_result=None,
        world_hash_before=before,
        world_hash_after=after,
        ledger_entries=len(ledger.entries),
        ledger_valid=bool(ledger.verify().valid),
    )


def _outcome_view(
    scenario: ScenarioId,
    route: Route,
    plan: Plan,
    result: ExecutionResult,
    replay_hash: str | None,
    objection: str,
    before: str,
    after: str,
    ledger: Ledger,
) -> DecisionView:
    resolved = result.outcome is ExecutionOutcome.RESOLVED
    reason = "" if result.escalation_reason is None else result.escalation_reason.value
    headline = (
        f"Exactly one procedure fits: {plan.category.value}. Executed deterministically."
        if resolved
        else _refusal_headline(scenario, reason, objection)
    )
    return DecisionView(
        decision=Decision.AUTOMATE if resolved else Decision.ESCALATE,
        headline=headline,
        route_kind=route.kind,
        route_reason=route.reason,
        route_detail=route.detail,
        co_holding_categories=(),
        plan_id=plan.plan_id,
        plan_version=plan.version,
        model_calls_classification=1,
        model_calls_after_classification=0,
        compiled_steps_executed=result.steps_completed,
        outcome=result.outcome.value,
        outcome_hash=result.outcome_hash,
        replay_match=None if replay_hash is None else replay_hash == result.outcome_hash,
        replay_outcome_hash=replay_hash,
        guard_objection=objection,
        handover_reason=reason,
        handover_step=None if result.handover is None else result.handover.step_index,
        quarantined_result=None if result.handover is None else result.handover.untrusted_result,
        world_hash_before=before,
        world_hash_after=after,
        ledger_entries=len(ledger.entries),
        ledger_valid=bool(ledger.verify().valid),
    )


def _refusal_headline(scenario: ScenarioId, reason: str, objection: str) -> str:
    if scenario is ScenarioId.CAP_BREACH:
        return f"The policy gate refused: the amount exceeds its per-action cap ({reason})."
    if scenario is ScenarioId.SCHEMA_DRIFT:
        return f"The guard rejected the result before it became state: {objection or reason}."
    return f"The compiled run stopped and handed over: {reason}."


def _clock() -> Callable[[], datetime]:
    moment = datetime(2026, 8, 23, 10, 0, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(seconds=1)
        return moment

    return tick


__all__ = [
    "SCENARIOS",
    "Decision",
    "DecisionView",
    "EvidenceView",
    "InvestigationView",
    "ScenarioId",
    "ScenarioResult",
    "ScenarioSpec",
    "run_scenario",
    "scenario_spec",
]
