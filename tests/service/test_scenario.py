import ast
import pathlib

import pytest

from rote.contracts.canonical import canonical_bytes
from rote.contracts.routing import RouteKind, RouteReason
from rote.service.scenario import (
    SCENARIOS,
    Decision,
    ScenarioId,
    ScenarioResult,
    run_scenario,
    scenario_spec,
)

SERVICE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "service"


@pytest.fixture(scope="module")
def automated() -> ScenarioResult:
    return run_scenario(ScenarioId.AUTOMATED)


@pytest.fixture(scope="module")
def ambiguous() -> ScenarioResult:
    return run_scenario(ScenarioId.AMBIGUOUS)


class TestTheFacadeIsThin:
    # the demo consumes ScenarioResult; it must never reach into the runtime itself
    def test_every_view_is_json_serialisable(self, automated: ScenarioResult) -> None:
        assert canonical_bytes(automated.model_dump(mode="json"))

    def test_the_facade_adds_no_reconciliation_logic(self) -> None:
        source = (SERVICE / "scenario.py").read_text(encoding="utf-8")
        for banned in ("minor_units *", "percentage_bps", "flat_fee", "anagram", "sorted(ours)"):
            assert banned not in source

    def test_it_defines_no_precondition_of_its_own(self) -> None:
        tree = ast.parse((SERVICE / "scenario.py").read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not any("precondition" in name for name in names)

    def test_every_declared_scenario_has_a_spec(self) -> None:
        assert set(SCENARIOS) == set(ScenarioId)
        for scenario in ScenarioId:
            spec = scenario_spec(scenario)
            assert spec.title and spec.question and spec.closing_line

    def test_a_spec_pins_its_seeds(self) -> None:
        spec = scenario_spec(ScenarioId.AUTOMATED)
        assert spec.eval_seed > 0
        assert spec.fit_seed > 0


class TestDeterminism:
    def test_the_same_scenario_yields_the_same_result_twice(self) -> None:
        first = run_scenario(ScenarioId.AMBIGUOUS)
        second = run_scenario(ScenarioId.AMBIGUOUS)
        assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
            second.model_dump(mode="json")
        )

    def test_the_chosen_exception_is_pinned_in_the_result(self, automated: ScenarioResult) -> None:
        assert automated.investigation.exception_id == automated.spec.exception_id


class TestTheInvestigationView:
    def test_it_carries_the_structured_facts(self, automated: ScenarioResult) -> None:
        assert automated.investigation.facts["record_id"]

    def test_untrusted_text_is_labelled_and_kept_apart(self, automated: ScenarioResult) -> None:
        for block in automated.investigation.untrusted:
            assert block.source_path.startswith("$.")
            assert block.content

    def test_no_untrusted_content_leaks_into_the_structured_facts(
        self, automated: ScenarioResult
    ) -> None:
        rendered = canonical_bytes(automated.investigation.facts).decode()
        for block in automated.investigation.untrusted:
            assert block.content not in rendered

    def test_every_recorded_call_carries_a_gate_verdict(self, automated: ScenarioResult) -> None:
        assert automated.investigation.trajectory
        for call in automated.investigation.trajectory:
            assert call.gate_verdict


class TestTheEvidenceView:
    def test_it_names_which_categories_fit_the_evidence(self, automated: ScenarioResult) -> None:
        assert automated.evidence.candidates
        fitting = [c.category for c in automated.evidence.candidates if c.precondition_holds]
        assert fitting == sorted(fitting)
        assert fitting == list(automated.evidence.fitting_categories)

    def test_an_unambiguous_case_has_exactly_one_fitting_category(
        self, automated: ScenarioResult
    ) -> None:
        assert len(automated.evidence.fitting_categories) == 1
        assert automated.evidence.ambiguous is False

    def test_an_ambiguous_case_names_more_than_one(self, ambiguous: ScenarioResult) -> None:
        assert len(ambiguous.evidence.fitting_categories) > 1
        assert ambiguous.evidence.ambiguous is True

    def test_a_compiled_case_shows_its_plan_provenance(self, automated: ScenarioResult) -> None:
        assert automated.evidence.plan is not None
        assert automated.evidence.plan.skeleton
        assert automated.evidence.plan.validation_passed is True
        assert automated.evidence.plan.holdout_size > 0
        assert automated.evidence.plan.approved_by.startswith("human:")
        for step in automated.evidence.plan.steps:
            for binding in step.bindings:
                assert binding.evidence_run_count >= 1

    def test_the_ledger_is_reported_with_its_verification(self, automated: ScenarioResult) -> None:
        assert automated.evidence.ledger_valid is True
        assert automated.evidence.ledger_entries > 0


class TestTheDecisionView:
    def test_an_unambiguous_case_automates(self, automated: ScenarioResult) -> None:
        assert automated.decision.decision is Decision.AUTOMATE
        assert automated.decision.route_kind is RouteKind.COMPILED_PLAN
        assert automated.decision.route_reason is RouteReason.PLAN_MATCHED
        assert automated.decision.model_calls_after_classification == 0
        assert automated.decision.outcome_hash

    def test_an_automated_case_replays_to_the_same_outcome(self, automated: ScenarioResult) -> None:
        assert automated.decision.replay_match is True
        assert automated.decision.replay_outcome_hash == automated.decision.outcome_hash

    def test_an_ambiguous_case_refuses(self, ambiguous: ScenarioResult) -> None:
        assert ambiguous.decision.decision is Decision.ESCALATE
        assert ambiguous.decision.route_kind is RouteKind.LIVE_AGENT
        assert ambiguous.decision.route_reason is RouteReason.AMBIGUOUS_EVIDENCE

    def test_a_refusal_names_the_competing_procedures(self, ambiguous: ScenarioResult) -> None:
        assert len(ambiguous.decision.co_holding_categories) > 1
        assert "Multiple procedures" in ambiguous.decision.headline


class TestRefusalIsSafe:
    def test_a_refusal_selects_no_plan(self, ambiguous: ScenarioResult) -> None:
        assert ambiguous.decision.plan_id is None
        assert ambiguous.decision.plan_version is None

    def test_a_refusal_never_consults_the_plan_source(self, ambiguous: ScenarioResult) -> None:
        assert ambiguous.evidence.plan_lookups == 0

    def test_a_refusal_executes_no_compiled_step(self, ambiguous: ScenarioResult) -> None:
        assert ambiguous.decision.compiled_steps_executed == 0

    def test_a_refusal_leaves_the_world_untouched_by_the_compiled_path(
        self, ambiguous: ScenarioResult
    ) -> None:
        assert ambiguous.decision.world_hash_before == ambiguous.decision.world_hash_after

    def test_an_automated_case_does_consult_the_plan_source(
        self, automated: ScenarioResult
    ) -> None:
        assert automated.evidence.plan_lookups == 1


class TestEveryScenarioRuns:
    @pytest.mark.parametrize("scenario", list(ScenarioId))
    def test_it_produces_all_three_views(self, scenario: ScenarioId) -> None:
        result = run_scenario(scenario)
        assert result.investigation.exception_id
        assert result.evidence.candidates
        assert result.decision.headline
        assert result.spec.id is scenario

    @pytest.mark.parametrize("scenario", list(ScenarioId))
    def test_every_result_declares_the_research_grade_caveat(self, scenario: ScenarioId) -> None:
        assert run_scenario(scenario).research_grade is False


class TestTheAdversarialScenarios:
    def test_an_injected_note_never_becomes_a_structured_fact(self) -> None:
        result = run_scenario(ScenarioId.INJECTED_NOTE)
        rendered = canonical_bytes(result.investigation.facts).decode()
        assert result.investigation.untrusted
        for block in result.investigation.untrusted:
            assert block.content not in rendered

    def test_schema_drift_is_caught_by_the_guard(self) -> None:
        result = run_scenario(ScenarioId.SCHEMA_DRIFT)
        assert result.decision.decision is Decision.ESCALATE
        assert result.decision.guard_objection

    def test_a_cap_breach_is_refused_at_the_gate(self) -> None:
        result = run_scenario(ScenarioId.CAP_BREACH)
        assert result.decision.decision is Decision.ESCALATE
        assert "cap" in result.decision.headline.lower()

    def test_a_killed_plan_is_no_longer_served(self) -> None:
        result = run_scenario(ScenarioId.KILL_SWITCH)
        assert result.decision.decision is Decision.ESCALATE
        assert result.decision.route_reason is RouteReason.NO_ACTIVE_PLAN
