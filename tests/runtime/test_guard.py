import ast
import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from rote.contracts.canonical import canonical_bytes
from rote.contracts.errors import GuardError
from rote.contracts.execution import EscalationReason, ExecutionOutcome
from rote.contracts.guard import GuardConfig, GuardSignal, GuardWeights
from rote.contracts.invariants import INVARIANTS, evaluate_invariants
from rote.contracts.plan import PlanStep, StepExpectation
from rote.runtime.guard import Guard, default_guard_config

RUNTIME_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "runtime"

CLEAN_RESULT: dict[str, Any] = {"amount": 500, "status": "settled"}
LEARNED_SCHEMA = frozenset({"$|object", "$.amount|int", "$.status|str"})


def expectation(**overrides: Any) -> StepExpectation:
    from rote.contracts.fingerprint import structural_fingerprint

    fields: dict[str, Any] = {
        "result_fingerprints": frozenset({structural_fingerprint(CLEAN_RESULT)}),
        "numeric_observed": {"$.amount": (400, 600)},
        "numeric_widened": {"$.amount": (300, 700)},
        "categorical_domains": {"$.status": frozenset({"settled", "pending"})},
        "invariants": (),
        "sample_count": 20,
        "schema_always": LEARNED_SCHEMA,
        "schema_ever": LEARNED_SCHEMA,
    }
    fields.update(overrides)
    return StepExpectation(**fields)


def step(index: int = 0, tool: str = "post_adjustment", **overrides: Any) -> PlanStep:
    return PlanStep(
        index=index,
        kind="TOOL_CALL",
        tool=tool,
        args=(),
        expect=expectation(**overrides),
    )


def guard(config: GuardConfig | None = None) -> Guard:
    return Guard(config=config or default_guard_config())


# a deliberately tighter threshold, so one structural break is enough to demonstrate
# the wiring; the approved defaults need two signals, which is a separate finding
def _sensitive() -> GuardConfig:
    return default_guard_config().model_copy(update={"threshold_per_mille": 300})


class TestEachSignalFiresOnItsOwnFixtureAndNothingElse:
    def test_a_clean_result_scores_zero_everywhere(self) -> None:
        verdict = guard().check_result(step(), CLEAN_RESULT)
        assert verdict.passed is True
        assert verdict.divergence_per_mille == 0
        assert all(score.score_per_mille == 0 for score in verdict.scores)

    def test_a_vanished_field_fires_structural_alone(self) -> None:
        verdict = guard().check_result(step(), {"status": "settled"})
        assert verdict.score_for(GuardSignal.STRUCTURAL) == 1000
        assert verdict.score_for(GuardSignal.CATEGORICAL) == 0
        assert verdict.score_for(GuardSignal.BEHAVIOURAL) == 0

    def test_a_changed_type_fires_structural_at_full_strength(self) -> None:
        verdict = guard().check_result(step(), {"amount": "500", "status": "settled"})
        assert verdict.score_for(GuardSignal.STRUCTURAL) == 1000

    def test_an_added_field_fires_structural_only_gently(self) -> None:
        verdict = guard().check_result(step(), {**CLEAN_RESULT, "surcharge": 12})
        assert verdict.score_for(GuardSignal.STRUCTURAL) == 400
        assert verdict.score_for(GuardSignal.NUMERIC) == 0

    def test_a_value_outside_the_widened_range_fires_numeric_alone(self) -> None:
        verdict = guard().check_result(step(), {"amount": 900, "status": "settled"})
        assert verdict.score_for(GuardSignal.NUMERIC) > 0
        assert verdict.score_for(GuardSignal.STRUCTURAL) == 0
        assert verdict.score_for(GuardSignal.CATEGORICAL) == 0

    def test_a_value_inside_the_widened_range_never_fires_numeric(self) -> None:
        verdict = guard().check_result(step(), {"amount": 690, "status": "settled"})
        assert verdict.score_for(GuardSignal.NUMERIC) == 0

    def test_numeric_scales_with_how_far_outside_the_value_is(self) -> None:
        near = guard().check_result(step(), {"amount": 720, "status": "settled"})
        far = guard().check_result(step(), {"amount": 5_000, "status": "settled"})
        assert far.score_for(GuardSignal.NUMERIC) > near.score_for(GuardSignal.NUMERIC)

    def test_a_wildly_wrong_amount_saturates_numeric(self) -> None:
        verdict = guard().check_result(step(), {"amount": 10_000_000, "status": "settled"})
        assert verdict.score_for(GuardSignal.NUMERIC) == 1000

    def test_an_unseen_enum_value_fires_categorical_alone(self) -> None:
        verdict = guard().check_result(step(), {"amount": 500, "status": "reversed"})
        assert verdict.score_for(GuardSignal.CATEGORICAL) == 1000
        assert verdict.score_for(GuardSignal.STRUCTURAL) == 0
        assert verdict.score_for(GuardSignal.NUMERIC) == 0

    def test_a_known_enum_value_never_fires_categorical(self) -> None:
        verdict = guard().check_result(step(), {"amount": 500, "status": "pending"})
        assert verdict.score_for(GuardSignal.CATEGORICAL) == 0

    def test_a_single_attempt_never_fires_behavioural(self) -> None:
        verdict = guard().check_result(step(), CLEAN_RESULT, attempts=1)
        assert verdict.score_for(GuardSignal.BEHAVIOURAL) == 0

    def test_a_retry_that_eventually_worked_fires_behavioural_gently(self) -> None:
        verdict = guard().check_result(step(), CLEAN_RESULT, attempts=2)
        assert verdict.score_for(GuardSignal.BEHAVIOURAL) == 300

    def test_every_signal_appears_in_the_vector_even_at_zero(self) -> None:
        verdict = guard().check_result(step(), CLEAN_RESULT)
        assert {score.signal for score in verdict.scores} == {
            GuardSignal.STRUCTURAL,
            GuardSignal.NUMERIC,
            GuardSignal.CATEGORICAL,
            GuardSignal.BEHAVIOURAL,
        }


class TestTheScoreCombination:
    def test_the_divergence_is_the_weighted_sum(self) -> None:
        verdict = guard().check_result(step(), {"status": "reversed"})
        expected = (1000 * 350 + 1000 * 250) // 1000
        assert verdict.divergence_per_mille == expected

    def test_a_small_divergence_still_passes(self) -> None:
        verdict = guard().check_result(step(), {**CLEAN_RESULT, "surcharge": 12})
        assert verdict.divergence_per_mille == 140
        assert verdict.passed is True

    def test_two_signals_together_cross_the_threshold_and_abort(self) -> None:
        verdict = guard().check_result(step(), {"status": "reversed"})
        assert verdict.divergence_per_mille >= verdict.threshold_per_mille
        assert verdict.passed is False

    def test_no_single_signal_can_abort_under_the_approved_defaults(self) -> None:
        # the heaviest weight is 350 and the threshold is 500, so one signal at full
        # strength scores 350 and is let through. Recorded as a calibration finding.
        weights = default_guard_config().weights
        heaviest = max(
            weights.structural, weights.numeric, weights.categorical, weights.behavioural
        )
        assert heaviest < default_guard_config().threshold_per_mille
        lone = guard().check_result(step(), {"status": "settled"})
        assert lone.score_for(GuardSignal.STRUCTURAL) == 1000
        assert lone.divergence_per_mille == 350
        assert lone.passed is True

    def test_the_threshold_used_is_recorded_on_the_verdict(self) -> None:
        verdict = guard().check_result(step(), CLEAN_RESULT)
        assert verdict.threshold_per_mille == default_guard_config().threshold_per_mille

    def test_weights_must_sum_to_a_full_scale(self) -> None:
        with pytest.raises(ValidationError):
            GuardWeights(structural=500, numeric=250, categorical=250, behavioural=250)

    def test_the_verdict_is_a_pure_function_of_its_inputs(self) -> None:
        first = guard().check_result(step(), {"amount": 900, "status": "reversed"})
        second = guard().check_result(step(), {"amount": 900, "status": "reversed"})
        assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
            second.model_dump(mode="json")
        )

    def test_a_looser_threshold_lets_the_same_divergence_through(self) -> None:
        loose = default_guard_config().model_copy(update={"threshold_per_mille": 1000})
        verdict = Guard(config=loose).check_result(step(), {"status": "settled"})
        assert verdict.passed is True
        assert verdict.divergence_per_mille > 0


class TestInvariantsVeto:
    def test_a_registered_invariant_that_holds_passes(self) -> None:
        verdict = guard().check_proposed_action(
            step(invariants=("adjustment_within_internal_amount",)),
            {"minor_units": 100, "currency": "INR"},
            {"internal_amount": {"minor_units": 5_000, "currency": "INR"}},
        )
        assert verdict.passed is True

    def test_an_adjustment_larger_than_the_record_is_vetoed(self) -> None:
        verdict = guard().check_proposed_action(
            step(invariants=("adjustment_within_internal_amount",)),
            {"minor_units": 99_999, "currency": "INR"},
            {"internal_amount": {"minor_units": 5_000, "currency": "INR"}},
        )
        assert verdict.passed is False
        assert verdict.vetoed is True
        assert verdict.failed_invariants == ("adjustment_within_internal_amount",)

    def test_a_veto_beats_any_threshold(self) -> None:
        never_abort = default_guard_config().model_copy(update={"threshold_per_mille": 1000})
        verdict = Guard(config=never_abort).check_proposed_action(
            step(invariants=("adjustment_within_internal_amount",)),
            {"minor_units": 99_999, "currency": "INR"},
            {"internal_amount": {"minor_units": 5_000, "currency": "INR"}},
        )
        assert verdict.passed is False
        assert verdict.vetoed is True

    def test_a_currency_mismatch_is_vetoed(self) -> None:
        verdict = guard().check_proposed_action(
            step(invariants=("adjustment_currency_matches_the_bank_line",)),
            {"minor_units": 100, "currency": "USD"},
            {"bank_amount": {"minor_units": 90, "currency": "INR"}},
        )
        assert verdict.vetoed is True

    def test_settling_against_a_line_that_was_never_a_candidate_is_vetoed(self) -> None:
        verdict = guard().check_proposed_action(
            step(invariants=("settles_against_a_candidate_line",)),
            {"bank_line_id": "BNK-999999"},
            {"candidate_bank_line_ids": ["BNK-000001", "BNK-000002"]},
        )
        assert verdict.vetoed is True

    def test_settling_against_a_real_candidate_passes(self) -> None:
        verdict = guard().check_proposed_action(
            step(invariants=("settles_against_a_candidate_line",)),
            {"bank_line_id": "BNK-000002"},
            {"candidate_bank_line_ids": ["BNK-000001", "BNK-000002"]},
        )
        assert verdict.passed is True

    def test_a_step_with_no_invariants_passes_the_pre_check(self) -> None:
        assert guard().check_proposed_action(step(), {"minor_units": 1}, {}).passed is True

    def test_an_unknown_invariant_name_raises_rather_than_being_skipped(self) -> None:
        with pytest.raises(GuardError):
            guard().check_proposed_action(step(invariants=("no_such_rule",)), {}, {})

    def test_the_registry_is_a_closed_set_of_named_checks(self) -> None:
        assert set(INVARIANTS) == {
            "adjustment_within_internal_amount",
            "adjustment_currency_matches_the_bank_line",
            "settles_against_a_candidate_line",
            "voids_only_a_candidate_line",
        }

    def test_a_missing_field_makes_an_invariant_fail_rather_than_pass(self) -> None:
        failed = evaluate_invariants(("adjustment_within_internal_amount",), {}, {})
        assert failed == ("adjustment_within_internal_amount",)


class TestTheRawVectorIsAlwaysRecorded:
    def test_a_passing_check_is_still_recorded(self) -> None:
        keeper = guard()
        keeper.check_result(step(), CLEAN_RESULT)
        assert len(keeper.inspections) == 1
        assert keeper.inspections[0].passed is True

    def test_the_pre_action_check_is_recorded_too(self) -> None:
        keeper = guard()
        keeper.check_proposed_action(step(), {"minor_units": 1}, {})
        assert keeper.inspections[0].checkpoint == "proposed_action"

    def test_both_checkpoints_are_distinguishable(self) -> None:
        keeper = guard()
        keeper.check_proposed_action(step(), {"minor_units": 1}, {})
        keeper.check_result(step(), CLEAN_RESULT)
        assert [v.checkpoint for v in keeper.inspections] == ["proposed_action", "result"]

    def test_every_recorded_verdict_carries_the_full_vector(self) -> None:
        keeper = guard()
        keeper.check_result(step(), CLEAN_RESULT)
        keeper.check_result(step(1), {"status": "reversed"})
        assert all(len(v.scores) == 4 for v in keeper.inspections)

    def test_the_recorded_verdicts_are_canonically_serialisable(self) -> None:
        keeper = guard()
        keeper.check_result(step(), {"amount": 900, "status": "reversed"})
        assert canonical_bytes([v.model_dump(mode="json") for v in keeper.inspections])

    def test_the_step_index_is_recorded(self) -> None:
        keeper = guard()
        keeper.check_result(step(3), CLEAN_RESULT)
        assert keeper.inspections[0].step_index == 3


class TestTheGuardCannotReachATool:
    def test_the_guard_holds_no_toolbox(self) -> None:
        assert not hasattr(guard(), "invoke")
        assert not hasattr(guard(), "available_tools")

    def test_the_guard_imports_no_adapter_and_no_gate(self) -> None:
        source = (RUNTIME_PACKAGE / "guard.py").read_text(encoding="utf-8")
        assert "domain.tools" not in source
        assert "safety.gate" not in source

    def test_the_guard_imports_no_model(self) -> None:
        banned = {"openai", "anthropic", "groq", "ollama", "langchain", "langgraph", "sklearn"}
        tree = ast.parse((RUNTIME_PACKAGE / "guard.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned

    def test_the_guard_never_evaluates_a_string(self) -> None:
        for name in ("guard.py",):
            tree = ast.parse((RUNTIME_PACKAGE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in {"eval", "exec", "compile", "getattr"}


class TestTheGuardDrivesTheExecutor:
    def _plan_and_toolbox(self, result: dict[str, Any]):  # type: ignore[no-untyped-def]
        from tests.runtime.test_executor import RecordingToolbox, active_plan

        plan = active_plan()
        toolbox = RecordingToolbox({"alpha": result, "beta": {"ok": 1}})
        return plan, toolbox

    def test_a_diverging_result_escalates_the_run(self) -> None:
        from rote.runtime.executor import execute_plan

        plan, toolbox = self._plan_and_toolbox({"surprise": None})
        keeper = Guard(config=_sensitive())
        result = execute_plan(
            plan=plan,
            task_input={"record_id": "REC-99"},
            toolbox=toolbox,
            inspector=keeper,
        )
        assert result.outcome is ExecutionOutcome.ESCALATED
        assert result.escalation_reason is EscalationReason.RESULT_DIVERGENCE

    def test_a_diverging_result_never_reaches_the_dependent_step(self) -> None:
        from rote.runtime.executor import execute_plan

        plan, toolbox = self._plan_and_toolbox({"surprise": None})
        execute_plan(
            plan=plan,
            task_input={"record_id": "REC-99"},
            toolbox=toolbox,
            inspector=Guard(config=_sensitive()),
        )
        assert [name for name, _args in toolbox.calls] == ["alpha"]

    def test_a_clean_run_is_unaffected_by_the_guard(self) -> None:
        from rote.runtime.executor import execute_plan

        plan, toolbox = self._plan_and_toolbox({"line_id": "BNK-99"})
        result = execute_plan(
            plan=plan,
            task_input={"record_id": "REC-99"},
            toolbox=toolbox,
            inspector=Guard(config=default_guard_config()),
        )
        assert result.outcome is ExecutionOutcome.RESOLVED
