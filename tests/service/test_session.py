import ast
import hashlib
import pathlib

import pytest

from rote.contracts.common import Currency, ExceptionCategory
from rote.contracts.ledger import LedgerEventType
from rote.contracts.policy import ExecutionPath, PolicyConfig, PolicyRule
from rote.contracts.routing import RouteKind, RouteReason
from rote.safety.policy_defaults import default_policy_config
from rote.service.scenario import Decision, compiled_system, demo_dataset
from rote.service.session import SessionRuntime, live_session

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINES = ROOT / "docs" / "baselines"
ORCHESTRATION = (ROOT / "rote" / "service", ROOT / "rote" / "web")


def fresh(killed: bool = False, policy: PolicyConfig | None = None) -> SessionRuntime:
    return SessionRuntime(
        system=compiled_system(killed=killed),
        dataset=demo_dataset(),
        policy=policy or default_policy_config(),
    )


@pytest.fixture(scope="module")
def session() -> SessionRuntime:
    return fresh()


def first_where(runtime: SessionRuntime, **match: object) -> str:
    for item in runtime.backlog():
        preview = runtime.preview(item.exception_id)
        if all(getattr(preview, key) == value for key, value in match.items()):
            return item.exception_id
    raise LookupError(f"no backlog case matching {match}")


# ---------------------------------------------------------------- A
class TestTheGuardCannotBeSilentlyDisabled:
    def test_every_orchestration_call_passes_an_inspector(self) -> None:
        offenders = []
        for package in ORCHESTRATION:
            for path in sorted(package.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name != "execute_plan":
                        continue
                    given = {kw.arg: kw.value for kw in node.keywords}
                    inspector = given.get("inspector")
                    if inspector is None or isinstance(inspector, ast.Constant):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"execute_plan without an inspector at {offenders}"

    def test_every_executed_step_was_inspected(self, session: SessionRuntime) -> None:
        runtime = fresh()
        target = first_where(runtime, route_reason=RouteReason.PLAN_MATCHED)
        resolution = runtime.resolve(target)
        assert resolution.compiled_steps_executed > 0
        assert resolution.guard_inspections >= resolution.compiled_steps_executed

    def test_a_rejecting_inspector_prevents_commit(self) -> None:
        runtime = fresh()
        target = first_where(runtime, route_reason=RouteReason.PLAN_MATCHED)
        before = runtime.world_view().world_hash
        resolution = runtime.resolve(target, reject_everything=True)
        assert resolution.decision is Decision.ESCALATE
        assert resolution.compiled_steps_executed == 0
        assert runtime.world_view().world_hash == before


# ---------------------------------------------------------------- B
class TestGateStatePersistsAcrossResolves:
    def test_resolving_twice_does_not_act_twice(self) -> None:
        runtime = fresh()
        target = first_where(runtime, route_reason=RouteReason.PLAN_MATCHED)

        first = runtime.resolve(target)
        after_first = runtime.world_view().world_hash
        assert first.decision is Decision.AUTOMATE
        assert first.world_changed is True

        second = runtime.resolve(target)
        assert second.already_resolved is True
        assert runtime.world_view().world_hash == after_first
        assert second.world_changed is False

    def test_no_duplicate_intent_or_outcome_entry(self) -> None:
        runtime = fresh()
        target = first_where(runtime, route_reason=RouteReason.PLAN_MATCHED)
        runtime.resolve(target)
        intents = runtime.count_events(target, LedgerEventType.INTENT)
        outcomes = runtime.count_events(target, LedgerEventType.OUTCOME)
        runtime.resolve(target)
        assert runtime.count_events(target, LedgerEventType.INTENT) == intents
        assert runtime.count_events(target, LedgerEventType.OUTCOME) == outcomes

    def test_the_session_holds_exactly_one_gate_and_one_ledger(
        self, session: SessionRuntime
    ) -> None:
        assert session.gate is session.gate
        assert session.ledger is session.ledger
        assert live_session() is live_session()


# ---------------------------------------------------------------- C
class TestRollingSpendPersistsAcrossResolves:
    def test_a_new_resolve_does_not_reset_the_window(self) -> None:
        narrow = _with_window_cap(2)
        runtime = fresh(policy=narrow)
        spent = 0
        refusals = 0
        for item in runtime.backlog()[:40]:
            resolution = runtime.resolve(item.exception_id)
            if resolution.decision is Decision.AUTOMATE:
                spent += 1
            elif resolution.handover_reason == "gate_cap_exceeded":
                refusals += 1
        assert refusals > 0, "the rolling window never bit, so it was not being carried"
        assert runtime.window_spend(Currency.INR) > 0


def _with_window_cap(cap: int) -> PolicyConfig:
    config = default_policy_config()
    rules = tuple(
        rule.model_copy(update={"max_per_window": dict.fromkeys(rule.max_per_window, cap)})
        if isinstance(rule, PolicyRule)
        else rule
        for rule in config.rules
    )
    return config.model_copy(update={"rules": rules})


# ---------------------------------------------------------------- D
class TestAmbiguousCasesNeverReachTheRegistry:
    def test_every_ambiguous_case_refuses_without_touching_a_plan(self) -> None:
        runtime = fresh()
        checked = 0
        for item in runtime.backlog()[:120]:
            preview = runtime.preview(item.exception_id)
            if preview.route_reason is not RouteReason.AMBIGUOUS_EVIDENCE:
                continue
            before_hash = runtime.world_view().world_hash
            before_entries = len(runtime.ledger.entries)
            resolution = runtime.resolve(item.exception_id)
            checked += 1
            assert resolution.decision is Decision.ESCALATE
            assert resolution.route_kind is RouteKind.LIVE_AGENT
            assert resolution.route_reason is RouteReason.AMBIGUOUS_EVIDENCE
            assert resolution.plan_lookups == 0
            assert resolution.compiled_steps_executed == 0
            assert resolution.plan_id is None
            assert len(runtime.ledger.entries) == before_entries
            assert runtime.world_view().world_hash == before_hash
            assert len(resolution.co_holding_categories) > 1
        assert checked > 0


# ---------------------------------------------------------------- E
class TestInactivePlansCannotExecute:
    def test_a_deactivated_plan_is_not_served(self) -> None:
        runtime = fresh(killed=True)
        target = first_where(runtime, route_reason=RouteReason.NO_ACTIVE_PLAN)
        before_hash = runtime.world_view().world_hash
        before_entries = len(runtime.ledger.entries)
        resolution = runtime.resolve(target)
        assert resolution.decision is Decision.ESCALATE
        assert resolution.route_reason is RouteReason.NO_ACTIVE_PLAN
        assert resolution.compiled_steps_executed == 0
        assert len(runtime.ledger.entries) == before_entries
        assert runtime.world_view().world_hash == before_hash


# ---------------------------------------------------------------- F
class TestUntrustedTextStaysUntrusted:
    def test_an_injected_note_changes_nothing(self) -> None:
        runtime = fresh()
        target = runtime.injected_note_case()
        detail = runtime.investigation(target)
        joined = "".join(block.content for block in detail.untrusted)
        assert "</merchant_note>" in joined
        rendered = str(detail.facts)
        for block in detail.untrusted:
            assert block.content not in rendered
        resolution = runtime.resolve(target)
        assert resolution.route_reason is RouteReason.PLAN_MATCHED
        for call in resolution.calls:
            for value in call.args.values():
                assert str(value) not in joined or str(value) == ""

    def test_the_classifier_is_local_only(self, session: SessionRuntime) -> None:
        assert session.classifier_is_local is True


# ---------------------------------------------------------------- G
class TestWorldMutatesOnlyAfterSafetyChecks:
    def test_a_rejected_result_never_becomes_committed_state(self) -> None:
        runtime = fresh()
        target = first_where(runtime, route_reason=RouteReason.PLAN_MATCHED)
        before = runtime.world_view()
        resolution = runtime.resolve(target, drift_on_step=0)
        assert resolution.decision is Decision.ESCALATE
        assert resolution.guard_objection
        assert resolution.compiled_steps_executed == 0
        assert runtime.world_view().world_hash == before.world_hash
        assert runtime.ledger_view().valid is True

    def test_a_cap_breach_commits_only_the_steps_that_passed(self) -> None:
        # the cap only bites on a tool with a declared money argument, so this needs a plan
        # that actually posts an adjustment rather than one that only marks a record matched
        runtime = fresh(policy=_with_action_cap(1))
        target = first_where(
            runtime,
            route_reason=RouteReason.PLAN_MATCHED,
            plan_id="reconciliation:fx_rounding",
        )
        resolution = runtime.resolve(target)
        assert resolution.decision is Decision.ESCALATE
        assert resolution.handover_reason == "gate_cap_exceeded"
        assert runtime.world_view().adjustments == 0
        assert runtime.ledger_view().valid is True


def _with_action_cap(cap: int) -> PolicyConfig:
    config = default_policy_config()
    rules = tuple(
        rule.model_copy(update={"max_per_action": dict.fromkeys(rule.max_per_action, cap)})
        for rule in config.rules
    )
    return config.model_copy(update={"rules": rules})


# ---------------------------------------------------------------- H
class TestTheResearchArtifactsAreImmutable:
    @pytest.mark.parametrize("baseline", ["phase16_v1", "phase16_v2"])
    def test_the_checksums_still_match(self, baseline: str) -> None:
        folder = BASELINES / baseline
        recorded = {}
        for line in (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
            digest, name = line.split(maxsplit=1)
            recorded[name.lstrip("*").strip()] = digest
        assert recorded, f"{baseline} has no recorded checksums"
        for name, digest in recorded.items():
            actual = hashlib.sha256((folder / name).read_bytes()).hexdigest()
            assert actual == digest, f"{baseline}/{name} changed"

    def test_no_orchestration_code_writes_any_file(self) -> None:
        # quoting the recorded figures is fine; opening a file for writing is not
        writers = {"write_text", "write_bytes", "unlink", "rename", "replace", "mkdir"}
        offenders = []
        for package in ORCHESTRATION:
            for path in sorted(package.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name in writers:
                        offenders.append(f"{path.name}:{node.lineno} {name}")
                    if name == "open":
                        offenders.append(f"{path.name}:{node.lineno} open")
        assert offenders == [], f"orchestration code writes files at {offenders}"


# ---------------------------------------------------------------- session shape
class TestTheSessionSurface:
    def test_the_backlog_is_stable_and_hides_ground_truth(self, session: SessionRuntime) -> None:
        first = [item.exception_id for item in session.backlog()]
        assert first == [item.exception_id for item in session.backlog()]
        assert len(first) == len(set(first))
        fields = set(session.backlog()[0].model_dump())
        assert "category" not in fields and "true_category" not in fields

    def test_an_unknown_exception_is_refused(self, session: SessionRuntime) -> None:
        with pytest.raises(LookupError):
            session.resolve("EXC-does-not-exist")

    def test_the_ledger_view_reports_its_own_verification(self, session: SessionRuntime) -> None:
        view = session.ledger_view()
        assert view.valid is True
        assert view.total == len(session.ledger.entries)

    def test_preview_never_mutates_anything(self, session: SessionRuntime) -> None:
        before_hash = session.world_view().world_hash
        before_entries = len(session.ledger.entries)
        for item in session.backlog()[:25]:
            session.preview(item.exception_id)
        assert session.world_view().world_hash == before_hash
        assert len(session.ledger.entries) == before_entries

    def test_a_refusal_names_the_competing_procedures(self, session: SessionRuntime) -> None:
        target = first_where(session, route_reason=RouteReason.AMBIGUOUS_EVIDENCE)
        preview = session.preview(target)
        assert len(preview.co_holding_categories) > 1
        assert list(preview.co_holding_categories) == sorted(preview.co_holding_categories)

    def test_the_live_path_uses_the_compiled_plan_route(self, session: SessionRuntime) -> None:
        counts = {reason: 0 for reason in RouteReason}
        for item in session.backlog()[:60]:
            counts[session.preview(item.exception_id).route_reason] += 1
        assert counts[RouteReason.PLAN_MATCHED] > 0
        assert counts[RouteReason.AMBIGUOUS_EVIDENCE] > 0

    def test_the_execution_path_is_declared_as_the_compiled_one(
        self, session: SessionRuntime
    ) -> None:
        assert session.execution_path is ExecutionPath.COMPILED_PLAN

    def test_the_categories_it_can_serve_are_the_generated_ones(
        self, session: SessionRuntime
    ) -> None:
        served = {plan.category for plan in session.registry.all_plans()}
        assert served <= set(ExceptionCategory)
