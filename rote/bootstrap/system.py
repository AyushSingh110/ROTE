from __future__ import annotations

import collections
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
from rote.contracts.common import Currency, Domain, ExceptionCategory, UntrustedText
from rote.contracts.errors import RegistryError
from rote.contracts.plan import BindingKind, Plan, PlanStatus, PolicyRequirement
from rote.contracts.policy import ExecutionPath, PolicyContext
from rote.contracts.reconciliation import GroundTruth, ReconciliationException
from rote.contracts.tools import Toolbox
from rote.contracts.trajectory import Trajectory
from rote.domain.checkers.reconciliation import check_outcome
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.recorder.recorder import TrajectoryRecorder
from rote.runtime.guard import Guard, default_guard_config
from rote.runtime.shadow import PlaybackToolbox, run_shadow
from rote.safety.gate import PolicyGate
from rote.safety.ledger import Ledger
from rote.safety.policy_defaults import default_policy_config

PLAN_POLICY = PolicyRequirement(
    allowed_tools=frozenset(), max_per_action={Currency.INR: 50_000, Currency.USD: 1_000}
)
REGISTRY_POLICY = RegistryPolicy(min_shadow_agreements=20, max_shadow_disagreements=0)
BUDGET = AgentBudget(max_steps=12, max_tool_errors=3)
SIGN_OFF = "reviewed the compiled steps against the shadow diff"
AGENT_SEED = 1
HOLDOUT_FRACTION = 0.3


@dataclass(frozen=True)
class CompiledSystem:
    registry: PlanRegistry
    ledger: Ledger
    active_plans: int
    slot_bindings: int


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


def compile_and_activate(*, fit_seed: int, count: int, exploration: float = 0.0) -> CompiledSystem:
    data = generate_dataset(seed=fit_seed, count=count)
    adapters = ReconciliationTools.from_snapshot(data.world)
    gate = session_gate(adapters)
    truths = {truth.exception_id: truth for truth in data.ground_truths}
    model = CountingModel(OfflineHeuristicModel(seed=AGENT_SEED, exploration=exploration))

    labelled: list[Trajectory] = []
    category_of: dict[Any, ExceptionCategory] = {}
    for exception in data.exceptions:
        trajectory, _calls = run_live(exception, live_toolbox(gate, exception), model)
        truth = truths[exception.exception_id]
        judged = judge_against_ground_truth(trajectory, exception, truth, adapters)
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
    registry = PlanRegistry(ledger=ledger, clock=ticks(), policy=REGISTRY_POLICY)
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


def judge_against_ground_truth(
    trajectory: Trajectory,
    exception: ReconciliationException,
    truth: GroundTruth,
    adapters: ReconciliationTools,
) -> Trajectory:
    result = check_outcome(exception.facts, truth, adapters.snapshot())
    return trajectory.model_copy(
        update={"checker_verdict": result.verdict, "checker_version": result.checker_version}
    )


def run_live(
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
        recorder=TrajectoryRecorder(clock=ticks()),
        budget=BUDGET,
        correlation_id=f"{exception.exception_id}:live",
    )
    return trajectory, model.calls - before


def live_toolbox(gate: PolicyGate, exception: ReconciliationException) -> Toolbox:
    return gate.for_task(policy_context(exception.exception_id, ExecutionPath.LIVE_AGENT, None))


def session_gate(adapters: ReconciliationTools, ledger: Ledger | None = None) -> PolicyGate:
    return PolicyGate(
        adapters=adapters,
        config=default_policy_config(),
        ledger=Ledger() if ledger is None else ledger,
        clock=ticks(),
    )


def policy_context(
    task: str, path: ExecutionPath, category: ExceptionCategory | None
) -> PolicyContext:
    return PolicyContext(
        task_id=task,
        correlation_id=f"{task}:{path.value}",
        path=path,
        category=category,
        actor="system:agent" if path is ExecutionPath.LIVE_AGENT else "system:executor",
    )


def ticks() -> Callable[[], datetime]:
    moment = datetime(2026, 8, 23, 10, 0, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(seconds=1)
        return moment

    return tick


__all__ = [
    "AGENT_SEED",
    "BUDGET",
    "HOLDOUT_FRACTION",
    "PLAN_POLICY",
    "REGISTRY_POLICY",
    "SIGN_OFF",
    "CompiledSystem",
    "CountingModel",
    "compile_and_activate",
    "live_toolbox",
    "policy_context",
    "run_live",
    "session_gate",
    "ticks",
]
