import inspect
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from rote.compiler.builder import build_plan
from rote.compiler.registry import (
    PlanRegistry,
    RegistryPolicy,
    lifecycle_from_ledger,
)
from rote.compiler.replay import validate_plan
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import RegistryError
from rote.contracts.ledger import LedgerEventType
from rote.contracts.plan import Plan, PlanStatus, PolicyRequirement
from rote.contracts.trajectory import Trajectory
from rote.safety.ledger import Ledger
from tests.compiler.builders import build_with_steps

POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha"}), max_per_action={Currency.INR: 50_000}
)
HUMAN = "human:ops-lead-42"
SMALL = RegistryPolicy(min_shadow_agreements=3, max_shadow_disagreements=0)


def ticks() -> Iterator[datetime]:
    moment = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(seconds=1)


def runs(count: int, offset: int = 0, tool: str = "alpha") -> list[Trajectory]:
    return [
        build_with_steps(
            f"t{offset + i}",
            [(tool, {"record_id": f"REC-{offset + i}"}, {"ok": 1})],
            task_input={"record_id": f"REC-{offset + i}"},
        )
        for i in range(count)
    ]


def make_plan(
    *,
    version: int = 1,
    category: ExceptionCategory = ExceptionCategory.FEE_MISMATCH,
    validated: bool = True,
    tool: str = "alpha",
) -> Plan:
    fit = runs(20, tool=tool)
    plan = build_plan(fit, domain=Domain.RECONCILIATION, category=category, policy=POLICY)
    plan = plan.model_copy(update={"version": version})
    holdout = runs(8, offset=500, tool=tool) if validated else runs(0, tool=tool)
    if not validated:
        broken = build_with_steps(
            "odd", [(tool, {"record_id": "NOPE"}, {"ok": 1})], task_input={"record_id": "OTHER"}
        )
        holdout = [broken]
    return plan.model_copy(update={"validation": validate_plan(plan, holdout)})


def new_registry(policy: RegistryPolicy = SMALL) -> tuple[PlanRegistry, Ledger]:
    ledger = Ledger()
    clock = ticks()
    return PlanRegistry(ledger=ledger, clock=lambda: next(clock), policy=policy), ledger


def promote(registry: PlanRegistry, plan: Plan, *, agreements: int = 3) -> Plan:
    registry.register(plan, actor="system:compiler")
    for _ in range(agreements):
        registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")
    return registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="diff reviewed")


class TestValidationIsMandatory:
    def test_a_plan_with_no_validation_report_is_refused(self) -> None:
        registry, _ledger = new_registry()
        plan = build_plan(
            runs(20),
            domain=Domain.RECONCILIATION,
            category=ExceptionCategory.FEE_MISMATCH,
            policy=POLICY,
        )
        assert plan.validation is None
        with pytest.raises(RegistryError):
            registry.register(plan, actor="system:compiler")

    def test_a_plan_that_failed_validation_lands_inactive(self) -> None:
        registry, _ledger = new_registry()
        stored = registry.register(make_plan(validated=False), actor="system:compiler")
        assert stored.status is PlanStatus.INACTIVE

    def test_a_plan_that_passed_validation_lands_in_shadow_not_active(self) -> None:
        registry, _ledger = new_registry()
        stored = registry.register(make_plan(), actor="system:compiler")
        assert stored.status is PlanStatus.SHADOW

    def test_a_failed_plan_can_never_be_activated(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan(validated=False)
        registry.register(plan, actor="system:compiler")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="please")

    def test_activation_offers_no_override_of_any_kind(self) -> None:
        rendered = str(inspect.signature(PlanRegistry.activate)).lower()
        for banned in ("force", "override", "skip", "bypass", "ignore"):
            assert banned not in rendered


class TestActivationNeedsAHumanAndEvidence:
    def test_a_shadow_plan_with_too_little_evidence_is_refused(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="looks fine")

    def test_a_system_actor_may_not_activate(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        for _ in range(3):
            registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor="system:auto", note="fine")

    def test_activation_requires_a_sign_off_note(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        for _ in range(3):
            registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="   ")

    def test_a_fully_evidenced_plan_activates(self) -> None:
        registry, _ledger = new_registry()
        activated = promote(registry, make_plan())
        assert activated.status is PlanStatus.ACTIVE
        assert activated.activated_by == HUMAN
        assert activated.activated_at is not None

    def test_a_plan_still_in_draft_cannot_be_activated(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="never registered")


class TestShadowMode:
    def test_shadow_observations_are_only_accepted_while_shadowing(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        with pytest.raises(RegistryError):
            registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")

    def test_a_disagreement_demotes_the_plan_out_of_shadow(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        registry.observe_shadow(plan.plan_id, plan.version, agreed=False, actor="system:shadow")
        assert registry.get(plan.plan_id, plan.version).status is PlanStatus.INACTIVE

    def test_a_demoted_plan_cannot_then_be_activated(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        registry.observe_shadow(plan.plan_id, plan.version, agreed=False, actor="system:shadow")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="anyway")

    def test_a_shadow_plan_is_never_served_as_active(self) -> None:
        registry, _ledger = new_registry()
        registry.register(make_plan(), actor="system:compiler")
        assert registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH) is None

    def test_a_shadow_plan_is_discoverable_for_shadow_running(self) -> None:
        registry, _ledger = new_registry()
        registry.register(make_plan(), actor="system:compiler")
        shadow = registry.shadow_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH)
        assert shadow is not None
        assert shadow.status is PlanStatus.SHADOW


class TestServingActivePlans:
    def test_an_activated_plan_is_served(self) -> None:
        registry, _ledger = new_registry()
        promote(registry, make_plan())
        served = registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH)
        assert served is not None
        assert served.status is PlanStatus.ACTIVE

    def test_nothing_is_served_for_a_category_with_no_plan(self) -> None:
        registry, _ledger = new_registry()
        promote(registry, make_plan())
        assert registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FX_ROUNDING) is None

    def test_every_served_plan_carries_a_passing_validation(self) -> None:
        registry, _ledger = new_registry()
        promote(registry, make_plan())
        served = registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH)
        assert served is not None
        assert served.validation is not None
        assert served.validation.passed is True

    def test_activating_a_new_version_retires_the_old_one(self) -> None:
        registry, _ledger = new_registry()
        first = promote(registry, make_plan(version=1))
        promote(registry, make_plan(version=2, tool="alpha"))
        assert registry.get(first.plan_id, 1).status is PlanStatus.RETIRED
        served = registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH)
        assert served is not None
        assert served.version == 2

    def test_only_one_plan_is_ever_active_for_a_category(self) -> None:
        registry, _ledger = new_registry()
        promote(registry, make_plan(version=1))
        promote(registry, make_plan(version=2))
        active = [p for p in registry.all_plans() if p.status is PlanStatus.ACTIVE]
        assert len(active) == 1


class TestKillSwitch:
    def test_an_active_plan_can_be_switched_off(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident 4417")
        assert registry.get(plan.plan_id, plan.version).status is PlanStatus.INACTIVE

    def test_a_switched_off_plan_is_no_longer_served(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        assert registry.active_for(Domain.RECONCILIATION, ExceptionCategory.FEE_MISMATCH) is None

    def test_the_system_may_pull_the_switch_without_a_human(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(
            plan.plan_id, plan.version, actor="system:guard", reason="escalation breach"
        )
        assert registry.get(plan.plan_id, plan.version).status is PlanStatus.INACTIVE

    def test_switching_off_requires_a_reason(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        with pytest.raises(RegistryError):
            registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="")

    def test_a_switched_off_plan_cannot_be_reactivated_without_shadowing_again(self) -> None:
        registry, _ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        with pytest.raises(RegistryError):
            registry.activate(plan.plan_id, plan.version, actor=HUMAN, note="put it back")


class TestTheLedgerIsTheRecord:
    def test_every_transition_is_written_down(self) -> None:
        registry, ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        kinds = [e.event_type for e in ledger.entries]
        assert LedgerEventType.PLAN_VALIDATED in kinds
        assert LedgerEventType.PLAN_SHADOWED in kinds
        assert LedgerEventType.PLAN_ACTIVATED in kinds
        assert LedgerEventType.PLAN_DEACTIVATED in kinds

    def test_the_activating_human_is_named_in_the_ledger(self) -> None:
        registry, ledger = new_registry()
        promote(registry, make_plan())
        activation = next(
            e for e in ledger.entries if e.event_type is LedgerEventType.PLAN_ACTIVATED
        )
        assert activation.actor == HUMAN
        assert activation.payload["note"] == "diff reviewed"

    def test_the_lifecycle_can_be_rebuilt_from_the_ledger_alone(self) -> None:
        registry, ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        history = lifecycle_from_ledger(ledger.entries, plan.plan_id)
        assert [t.to_status for t in history] == [
            PlanStatus.SHADOW,
            PlanStatus.ACTIVE,
            PlanStatus.INACTIVE,
        ]

    def test_the_rebuilt_history_names_every_actor(self) -> None:
        registry, ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        history = lifecycle_from_ledger(ledger.entries, plan.plan_id)
        assert [t.actor for t in history] == ["system:compiler", HUMAN, HUMAN]

    def test_the_chain_stays_valid_across_a_whole_lifecycle(self) -> None:
        registry, ledger = new_registry()
        plan = promote(registry, make_plan())
        registry.deactivate(plan.plan_id, plan.version, actor=HUMAN, reason="incident")
        assert ledger.verify().valid is True

    def test_the_history_of_another_plan_is_not_mixed_in(self) -> None:
        registry, ledger = new_registry()
        promote(registry, make_plan(category=ExceptionCategory.FEE_MISMATCH))
        promote(registry, make_plan(category=ExceptionCategory.FX_ROUNDING))
        history = lifecycle_from_ledger(ledger.entries, "reconciliation:fx_rounding")
        assert all(t.plan_id == "reconciliation:fx_rounding" for t in history)


class TestRegistryIsInert:
    def test_the_registry_never_activates_anything_by_itself(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        for _ in range(50):
            registry.observe_shadow(plan.plan_id, plan.version, agreed=True, actor="system:shadow")
        assert registry.get(plan.plan_id, plan.version).status is PlanStatus.SHADOW

    def test_the_registry_imports_no_runtime_or_model(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path("rote/compiler/registry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned = {"sklearn", "torch", "openai", "anthropic", "langgraph", "langchain"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned
                assert not node.module.startswith("rote.runtime")

    def test_registering_the_same_version_twice_is_refused(self) -> None:
        registry, _ledger = new_registry()
        plan = make_plan()
        registry.register(plan, actor="system:compiler")
        with pytest.raises(RegistryError):
            registry.register(plan, actor="system:compiler")

    def test_an_unknown_plan_cannot_be_fetched(self) -> None:
        registry, _ledger = new_registry()
        with pytest.raises(RegistryError):
            registry.get("nothing:here", 1)


def all_statuses(plans: Sequence[Plan]) -> set[PlanStatus]:
    return {plan.status for plan in plans}
