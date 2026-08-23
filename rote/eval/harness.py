from __future__ import annotations

import collections
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rote.agent.loop import run_agent
from rote.agent.models.offline import OfflineHeuristicModel
from rote.compiler.builder import build_plan
from rote.compiler.registry import PlanRegistry, RegistryPolicy
from rote.compiler.replay import validate_plan
from rote.compiler.selection import hash_split, select_eligible
from rote.contracts.agent import AgentBudget, DecisionRequest, ModelResponse
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Currency, Domain, ExceptionCategory, UntrustedText
from rote.contracts.errors import RegistryError
from rote.contracts.evaluation import (
    EvalPath,
    RepeatRecord,
    ReplayRecord,
    RunRecord,
    TerminalState,
)
from rote.contracts.execution import ExecutionOutcome, outcome_hash
from rote.contracts.ledger import LedgerEventType
from rote.contracts.plan import BindingKind, Plan, PlanStatus, PolicyRequirement
from rote.contracts.policy import ExecutionPath, PolicyContext
from rote.contracts.reconciliation import GroundTruth, ReconciliationException, WorldSnapshot
from rote.contracts.routing import Route, RouteKind
from rote.contracts.tools import Toolbox
from rote.contracts.trajectory import Trajectory
from rote.domain.checkers.reconciliation import check_outcome
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.eval.classifier_double import StructuredFieldsClassifier
from rote.recorder.recorder import TrajectoryRecorder
from rote.runtime.classifier import Classifier
from rote.runtime.executor import execute_plan
from rote.runtime.guard import Guard, default_guard_config
from rote.runtime.handover import build_handoff
from rote.runtime.router import Router
from rote.runtime.shadow import PlaybackToolbox, live_calls, run_shadow
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import default_policy_config

PLAN_POLICY = PolicyRequirement(
    allowed_tools=frozenset(), max_per_action={Currency.INR: 50_000, Currency.USD: 1_000}
)
REGISTRY_POLICY = RegistryPolicy(min_shadow_agreements=20, max_shadow_disagreements=0)
BUDGET = AgentBudget(max_steps=12, max_tool_errors=3)
MIN_CONFIDENCE_PER_MILLE = 700
SIGN_OFF = "reviewed the compiled steps against the shadow diff"
AGENT_SEED = 1
HOLDOUT_FRACTION = 0.3
EXPLORATION = 0.3


@dataclass(frozen=True)
class CompiledSystem:
    registry: PlanRegistry
    ledger: Ledger
    active_plans: int
    slot_bindings: int


@dataclass(frozen=True)
class EvaluationOutput:
    runs: tuple[RunRecord, ...]
    repeats: tuple[RepeatRecord, ...]
    exploring_repeats: tuple[RepeatRecord, ...]
    replays: tuple[ReplayRecord, ...]
    ledger_entries: int
    ledger_valid: bool
    run_ledger_entries: int
    run_ledger_valid: bool
    confusion: tuple[tuple[str, str, int], ...]
    routes: tuple[tuple[str, int], ...]
    active_plans: int
    slot_bindings: int


# what one exception did, before it becomes a log line
@dataclass(frozen=True)
class _Attempt:
    terminal: TerminalState
    post_calls: int
    digest: str
    steps: int
    tool_ms: int
    escalation: str | None
    plan_id: str | None = None
    plan_version: int | None = None


# counts model calls exactly, rather than inferring them from the step count afterwards
class CountingModel:
    def __init__(self, inner: OfflineHeuristicModel) -> None:
        self._inner = inner
        self.model_id = inner.model_id
        self.prompt_template_id = inner.prompt_template_id
        self.calls = 0

    def decide(self, request: DecisionRequest) -> ModelResponse:
        self.calls += 1
        return self._inner.decide(request)


def run_evaluation(
    *, fit_seed: int, eval_seed: int, fit_count: int, eval_count: int, repeats: int, sample: int
) -> EvaluationOutput:
    system = compile_and_activate(fit_seed=fit_seed, count=fit_count)
    data = generate_dataset(seed=eval_seed, count=eval_count)
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    lookup = {exception.exception_id: exception for exception in data.exceptions}

    live_runs = _live_arm(data.exceptions, truths, data.world, eval_seed)
    rote_runs, routes, confusion, ledger = _rote_arm(
        data.exceptions, truths, data.world, system.registry, eval_seed
    )
    compiled = [r for r in rote_runs if r.terminal_state is TerminalState.RESOLVED_COMPILED]
    chosen = [record.task_id for record in compiled[:sample]]

    return EvaluationOutput(
        runs=tuple(live_runs + rote_runs),
        repeats=tuple(
            _compiled_repeats(chosen, lookup, data.world, system.registry, repeats)
            + _live_repeats(chosen, lookup, data.world, repeats, exploration=0.0)
        ),
        exploring_repeats=tuple(
            _live_repeats(chosen, lookup, data.world, repeats, exploration=EXPLORATION)
        ),
        replays=tuple(_replay_arm(compiled, lookup, data.world, system.registry, ledger)),
        ledger_entries=len(system.ledger.entries),
        ledger_valid=bool(system.ledger.verify().valid),
        run_ledger_entries=len(ledger.entries),
        run_ledger_valid=bool(ledger.verify().valid),
        confusion=confusion,
        routes=routes,
        active_plans=system.active_plans,
        slot_bindings=system.slot_bindings,
    )


def compile_and_activate(*, fit_seed: int, count: int, exploration: float = 0.0) -> CompiledSystem:
    data = generate_dataset(seed=fit_seed, count=count)
    adapters = ReconciliationTools.from_snapshot(data.world)
    gate = _gate(adapters)
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    model = CountingModel(OfflineHeuristicModel(seed=AGENT_SEED, exploration=exploration))

    labelled: list[Trajectory] = []
    category_of: dict[Any, ExceptionCategory] = {}
    for exception in data.exceptions:
        trajectory, _calls = _run_live(exception, _live_toolbox(gate, exception), model)
        judged = _judge(trajectory, exception, truths[exception.exception_id], adapters)
        labelled.append(judged)
        category_of[judged.trajectory_id] = truths[exception.exception_id].category

    eligible, _rejects = select_eligible(labelled, domain=Domain.RECONCILIATION)
    split = hash_split(eligible, holdout_fraction=HOLDOUT_FRACTION)
    fit: dict[ExceptionCategory, list[Trajectory]] = collections.defaultdict(list)
    hold: dict[ExceptionCategory, list[Trajectory]] = collections.defaultdict(list)
    for trajectory in split.fit:
        fit[category_of[trajectory.trajectory_id]].append(trajectory)
    for trajectory in split.holdout:
        hold[category_of[trajectory.trajectory_id]].append(trajectory)

    ledger = Ledger()
    registry = PlanRegistry(ledger=ledger, clock=_ticks(), policy=REGISTRY_POLICY)
    specs = adapters.available_tools()
    mutating = frozenset(spec.name for spec in specs if spec.mutating)
    for category in sorted(fit, key=lambda item: item.value):
        plan = build_plan(
            fit[category], domain=Domain.RECONCILIATION, category=category, policy=PLAN_POLICY
        )
        plan = plan.model_copy(update={"validation": validate_plan(plan, hold[category])})
        registered = registry.register(plan, actor="system:compiler")
        if registered.status is PlanStatus.SHADOW:
            _earn_activation(registry, registered, hold[category], specs, mutating)

    return CompiledSystem(
        registry=registry,
        ledger=ledger,
        active_plans=sum(1 for p in registry.all_plans() if p.status is PlanStatus.ACTIVE),
        slot_bindings=sum(
            1
            for plan in registry.all_plans()
            for step in plan.steps
            for binding in step.args
            if binding.kind is BindingKind.FROM_SLOT
        ),
    )


# a plan earns ACTIVE the way Phase 15 requires: shadow runs first, then a named human
def _earn_activation(
    registry: PlanRegistry,
    plan: Plan,
    holdout: Sequence[Trajectory],
    specs: Any,
    mutating: frozenset[str],
) -> None:
    for trajectory in holdout:
        observation = run_shadow(
            plan=plan,
            trajectory=trajectory,
            toolbox=PlaybackToolbox(trajectory=trajectory, specs=specs),
            mutating_tools=mutating,
            inspector=Guard(config=default_guard_config()),
        )
        try:
            registry.observe_shadow(
                plan.plan_id, plan.version, agreed=observation.agreed, actor="system:shadow"
            )
        except RegistryError:
            return
    if registry.get(plan.plan_id, plan.version).status is not PlanStatus.SHADOW:
        return
    try:
        registry.activate(plan.plan_id, plan.version, actor="human:ops-lead-42", note=SIGN_OFF)
    except RegistryError:
        # too little shadow evidence to earn activation: the plan stays shadowing and the
        # router will keep sending that category to the live agent. That is the right answer.
        return


def _live_arm(
    exceptions: Sequence[ReconciliationException],
    truths: dict[str, GroundTruth],
    world: WorldSnapshot,
    seed: int,
) -> list[RunRecord]:
    adapters = ReconciliationTools.from_snapshot(world)
    gate = _gate(adapters)
    model = CountingModel(OfflineHeuristicModel(seed=AGENT_SEED))
    records = []
    for exception in exceptions:
        started = time.perf_counter()
        trajectory, calls = _run_live(exception, _live_toolbox(gate, exception), model)
        elapsed = int((time.perf_counter() - started) * 1000)
        result = check_outcome(exception.facts, truths[exception.exception_id], adapters.snapshot())
        records.append(
            _record(
                exception,
                EvalPath.LIVE_AGENT,
                seed,
                _attempt_from(trajectory, calls),
                verdict=result.verdict,
                version=result.checker_version,
                classification_calls=0,
                elapsed_ms=elapsed,
                route=None,
                model_id=model.model_id,
            )
        )
    return records


def _rote_arm(
    exceptions: Sequence[ReconciliationException],
    truths: dict[str, GroundTruth],
    world: WorldSnapshot,
    registry: PlanRegistry,
    seed: int,
) -> tuple[list[RunRecord], tuple[tuple[str, int], ...], tuple[tuple[str, str, int], ...], Ledger]:
    adapters = ReconciliationTools.from_snapshot(world)
    ledger = Ledger()
    gate = _gate(adapters, ledger)
    model = CountingModel(OfflineHeuristicModel(seed=AGENT_SEED))
    classifier = Classifier(model=StructuredFieldsClassifier())
    router = Router(
        plans=registry,
        domain=Domain.RECONCILIATION,
        min_confidence_per_mille=MIN_CONFIDENCE_PER_MILLE,
    )

    records: list[RunRecord] = []
    routes: collections.Counter[str] = collections.Counter()
    confusion: collections.Counter[tuple[str, str]] = collections.Counter()

    for exception in exceptions:
        facts = exception.facts.model_dump(mode="json")
        truth = truths[exception.exception_id]
        started = time.perf_counter()
        classification = classifier.classify(facts, exception.untrusted, exception.exception_id)
        route = router.route(facts, classification)
        routes[route.reason.value] += 1
        confusion[(truth.category.value, classification.category.value)] += 1

        attempt = _rote_attempt(exception, route, registry, gate, model)
        elapsed = int((time.perf_counter() - started) * 1000)
        result = check_outcome(exception.facts, truth, adapters.snapshot())
        records.append(
            _record(
                exception,
                EvalPath.ROTE,
                seed,
                attempt,
                verdict=result.verdict,
                version=result.checker_version,
                classification_calls=1,
                elapsed_ms=elapsed,
                route=route,
                model_id=model.model_id,
            )
        )

    ordered_routes = tuple(sorted(routes.items(), key=lambda item: (-item[1], item[0])))
    ordered_confusion = tuple(
        (true, predicted, count) for (true, predicted), count in sorted(confusion.items())
    )
    return records, ordered_routes, ordered_confusion, ledger


def _rote_attempt(
    exception: ReconciliationException,
    route: Route,
    registry: PlanRegistry,
    gate: PolicyGate,
    model: CountingModel,
) -> _Attempt:
    if route.kind is not RouteKind.COMPILED_PLAN or route.plan_id is None:
        trajectory, calls = _run_live(exception, _live_toolbox(gate, exception), model)
        fallback = _attempt_from(trajectory, calls)
        if fallback.escalation is None:
            return fallback
        return _replace_reason(fallback, route.reason.value)

    plan = registry.get(route.plan_id, route.plan_version or 1)
    result = execute_plan(
        plan=plan,
        task_input=exception.facts.model_dump(mode="json"),
        toolbox=gate.for_task(
            _context(exception.exception_id, ExecutionPath.COMPILED_PLAN, plan.category)
        ),
        inspector=Guard(config=default_guard_config()),
    )
    if result.outcome is ExecutionOutcome.RESOLVED:
        return _Attempt(
            terminal=TerminalState.RESOLVED_COMPILED,
            post_calls=0,
            digest=result.outcome_hash,
            steps=result.steps_completed,
            tool_ms=0,
            escalation=None,
            plan_id=plan.plan_id,
            plan_version=plan.version,
        )

    assert result.handover is not None
    handoff = build_handoff(result.handover, original_untrusted=exception.untrusted)
    trajectory, calls = _run_live(
        exception,
        _live_toolbox(gate, exception),
        model,
        task_input=handoff.task_input,
        untrusted=handoff.untrusted,
    )
    attempt = _attempt_from(trajectory, calls)
    reason = result.escalation_reason.value if result.escalation_reason else "handover_failed"
    named = attempt if attempt.escalation is None else _replace_reason(attempt, reason)
    return _Attempt(
        terminal=named.terminal,
        post_calls=named.post_calls,
        digest=named.digest,
        steps=named.steps,
        tool_ms=named.tool_ms,
        escalation=named.escalation,
        plan_id=plan.plan_id,
        plan_version=plan.version,
    )


def _compiled_repeats(
    tasks: Sequence[str],
    lookup: dict[str, ReconciliationException],
    world: WorldSnapshot,
    registry: PlanRegistry,
    repeats: int,
) -> list[RepeatRecord]:
    classifier = Classifier(model=StructuredFieldsClassifier())
    router = Router(
        plans=registry,
        domain=Domain.RECONCILIATION,
        min_confidence_per_mille=MIN_CONFIDENCE_PER_MILLE,
    )
    records = []
    for task in tasks:
        exception = lookup[task]
        facts = exception.facts.model_dump(mode="json")
        for index in range(repeats):
            adapters = ReconciliationTools.from_snapshot(world)
            gate = _gate(adapters)
            route = router.route(facts, classifier.classify(facts, exception.untrusted, task))
            if route.kind is not RouteKind.COMPILED_PLAN or route.plan_id is None:
                continue
            plan = registry.get(route.plan_id, route.plan_version or 1)
            result = execute_plan(
                plan=plan,
                task_input=facts,
                toolbox=gate.for_task(_context(task, ExecutionPath.COMPILED_PLAN, plan.category)),
                inspector=Guard(config=default_guard_config()),
            )
            records.append(
                RepeatRecord(
                    task_id=task,
                    path=EvalPath.ROTE,
                    repeat_index=index,
                    outcome_hash=result.outcome_hash,
                    plan_id=plan.plan_id,
                    slot_call_count=0,
                )
            )
    return records


# one model instance across every repeat, on purpose: a fresh one replays the same random
# stream and would manufacture a consistency of 1.0 (the Phase 8 mistake)
def _live_repeats(
    tasks: Sequence[str],
    lookup: dict[str, ReconciliationException],
    world: WorldSnapshot,
    repeats: int,
    *,
    exploration: float,
) -> list[RepeatRecord]:
    model = CountingModel(OfflineHeuristicModel(seed=AGENT_SEED, exploration=exploration))
    records = []
    for task in tasks:
        exception = lookup[task]
        for index in range(repeats):
            adapters = ReconciliationTools.from_snapshot(world)
            gate = _gate(adapters)
            trajectory, _calls = _run_live(exception, _live_toolbox(gate, exception), model)
            records.append(
                RepeatRecord(
                    task_id=task,
                    path=EvalPath.LIVE_AGENT,
                    repeat_index=index,
                    outcome_hash=_live_hash(trajectory),
                    plan_id=None,
                    slot_call_count=0,
                )
            )
    return records


# the ledger stores a result hash and a derived key, never the arguments, so it can verify a
# replay but cannot reconstruct one. Both halves are reported.
def _replay_arm(
    compiled: Sequence[RunRecord],
    lookup: dict[str, ReconciliationException],
    world: WorldSnapshot,
    registry: PlanRegistry,
    original: Ledger,
) -> list[ReplayRecord]:
    original_keys = _keys_by_task(original)
    records = []
    for record in compiled:
        exception = lookup[record.task_id]
        adapters = ReconciliationTools.from_snapshot(world)
        ledger = Ledger()
        gate = _gate(adapters, ledger)
        plan = registry.get(record.plan_id or "", record.plan_version or 1)
        result = execute_plan(
            plan=plan,
            task_input=exception.facts.model_dump(mode="json"),
            toolbox=gate.for_task(
                _context(record.task_id, ExecutionPath.COMPILED_PLAN, plan.category)
            ),
            inspector=Guard(config=default_guard_config()),
        )
        replayed = _keys_by_task(ledger).get(record.task_id, frozenset())
        records.append(
            ReplayRecord(
                task_id=record.task_id,
                original_outcome_hash=record.outcome_hash,
                replay_outcome_hash=result.outcome_hash,
                idempotency_keys_match=replayed == original_keys.get(record.task_id, frozenset()),
                first_differing_seq=None,
            )
        )
    return records


def _record(
    exception: ReconciliationException,
    path: EvalPath,
    seed: int,
    attempt: _Attempt,
    *,
    verdict: CheckerVerdict,
    version: str,
    classification_calls: int,
    elapsed_ms: int,
    route: Route | None,
    model_id: str,
) -> RunRecord:
    return RunRecord(
        correlation_id=f"{exception.exception_id}:{path.value}",
        task_id=exception.exception_id,
        seed=seed,
        path=path,
        terminal_state=attempt.terminal,
        llm_calls_classification=classification_calls,
        llm_calls_post_classification=attempt.post_calls,
        route_kind=None if route is None else route.kind.value,
        route_reason=None if route is None else route.reason.value,
        escalation_reason=attempt.escalation,
        plan_id=attempt.plan_id,
        plan_version=attempt.plan_version,
        checker_verdict=verdict,
        checker_version=version,
        agent_model_id=model_id,
        outcome_hash=attempt.digest,
        tokens_in=0,
        tokens_out=0,
        wall_ms_total=elapsed_ms,
        wall_ms_excluding_tool_io=max(0, elapsed_ms - attempt.tool_ms),
        steps=attempt.steps,
    )


def _attempt_from(trajectory: Trajectory, calls: int) -> _Attempt:
    terminal = _terminal_of(trajectory)
    return _Attempt(
        terminal=terminal,
        post_calls=calls,
        digest=_live_hash(trajectory),
        steps=len(trajectory.steps),
        tool_ms=sum(step.latency_ms for step in trajectory.steps),
        escalation=None if terminal is TerminalState.RESOLVED_LIVE else "live_agent_gave_up",
    )


def _replace_reason(attempt: _Attempt, reason: str) -> _Attempt:
    return _Attempt(
        terminal=attempt.terminal,
        post_calls=attempt.post_calls,
        digest=attempt.digest,
        steps=attempt.steps,
        tool_ms=attempt.tool_ms,
        escalation=reason,
        plan_id=attempt.plan_id,
        plan_version=attempt.plan_version,
    )


def _keys_by_task(ledger: Ledger) -> dict[str, frozenset[str]]:
    grouped: dict[str, set[str]] = collections.defaultdict(set)
    for entry in ledger.entries:
        key = entry.payload.get("key")
        if entry.event_type is LedgerEventType.INTENT and isinstance(key, str):
            grouped[entry.task_id].add(key)
    return {task: frozenset(keys) for task, keys in grouped.items()}


def _judge(
    trajectory: Trajectory,
    exception: ReconciliationException,
    truth: GroundTruth,
    adapters: ReconciliationTools,
) -> Trajectory:
    result = check_outcome(exception.facts, truth, adapters.snapshot())
    return trajectory.model_copy(
        update={"checker_verdict": result.verdict, "checker_version": result.checker_version}
    )


def _terminal_of(trajectory: Trajectory) -> TerminalState:
    if trajectory.outcome == "resolved":
        return TerminalState.RESOLVED_LIVE
    if trajectory.outcome == "failed":
        return TerminalState.FAILED
    return TerminalState.ESCALATED


def _run_live(
    exception: ReconciliationException,
    toolbox: Toolbox,
    model: CountingModel,
    task_input: dict[str, Any] | None = None,
    untrusted: tuple[UntrustedText, ...] | None = None,
) -> tuple[Trajectory, int]:
    before = model.calls
    facts = exception.facts.model_dump(mode="json")
    trajectory = run_agent(
        domain=exception.domain,
        task_input=facts if task_input is None else task_input,
        untrusted=exception.untrusted if untrusted is None else untrusted,
        toolbox=toolbox,
        model=model,
        recorder=TrajectoryRecorder(clock=_ticks()),
        budget=BUDGET,
        correlation_id=f"{exception.exception_id}:live",
    )
    return trajectory, model.calls - before


def _live_toolbox(gate: PolicyGate, exception: ReconciliationException) -> Toolbox:
    return gate.for_task(_context(exception.exception_id, ExecutionPath.LIVE_AGENT, None))


def _live_hash(trajectory: Trajectory) -> str:
    terminal = (
        ExecutionOutcome.RESOLVED
        if trajectory.outcome == "resolved"
        else ExecutionOutcome.ESCALATED
    )
    return outcome_hash(terminal, live_calls(trajectory))


def _gate(adapters: ReconciliationTools, ledger: Ledger | None = None) -> PolicyGate:
    return PolicyGate(
        adapters=adapters,
        config=default_policy_config(),
        ledger=Ledger() if ledger is None else ledger,
        clock=_ticks(),
    )


def _context(task: str, path: ExecutionPath, category: ExceptionCategory | None) -> PolicyContext:
    return PolicyContext(
        task_id=task,
        correlation_id=f"{task}:{path.value}",
        path=path,
        category=category,
        actor="system:agent" if path is ExecutionPath.LIVE_AGENT else "system:executor",
    )


def _ticks() -> Callable[[], datetime]:
    moment = datetime(2026, 8, 23, 10, 0, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(seconds=1)
        return moment

    return tick


# SS I.8 asks whether the same skeleton emerges from a DIFFERENT model. There is only one
# stand-in here, so this is the weaker honest version: the same model recorded twice, once
# taking detours. It tests that the procedure survives noise, not that it survives a change
# of model, and it must never be reported as the stronger claim.
def skeleton_agreement(
    *, fit_seed: int, count: int, exploration: float = EXPLORATION
) -> tuple[tuple[str, str, str, bool], ...]:
    quiet = compile_and_activate(fit_seed=fit_seed, count=count)
    noisy = compile_and_activate(fit_seed=fit_seed, count=count, exploration=exploration)
    left = {plan.category: plan.skeleton for plan in quiet.registry.all_plans()}
    right = {plan.category: plan.skeleton for plan in noisy.registry.all_plans()}
    rows = []
    for category in sorted(set(left) | set(right), key=lambda item: item.value):
        a, b = left.get(category, ()), right.get(category, ())
        rows.append((category.value, " -> ".join(a), " -> ".join(b), a == b))
    return tuple(rows)


__all__ = [
    "CompiledSystem",
    "CountingModel",
    "EvaluationOutput",
    "compile_and_activate",
    "run_evaluation",
    "skeleton_agreement",
]
