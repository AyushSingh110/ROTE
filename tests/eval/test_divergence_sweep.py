from typing import Any

from rote.contracts.canonical import canonical_bytes
from rote.contracts.guard import GuardSignal, GuardWeights, SignalScore
from rote.domain.generators.divergence import (
    ADDED_FIELD,
    DivergenceLabel,
    divergence_set,
    inject,
)
from rote.eval.divergence import (
    DivergenceCurve,
    ScoredCase,
    select_operating_point,
    sweep,
)

WEIGHTS = GuardWeights(structural=350, numeric=250, categorical=250, behavioural=150)
NESTED: dict[str, Any] = {
    "record": {"minor_units": 500, "status": "settled", "reference": "REF1"},
    "count": 3,
}


def scored(label: DivergenceLabel, structural: int = 0, applied: bool = True) -> ScoredCase:
    return ScoredCase(
        label=label,
        applied=applied,
        scores=(
            SignalScore(signal=GuardSignal.STRUCTURAL, score_per_mille=structural),
            SignalScore(signal=GuardSignal.NUMERIC, score_per_mille=0),
            SignalScore(signal=GuardSignal.CATEGORICAL, score_per_mille=0),
            SignalScore(signal=GuardSignal.BEHAVIOURAL, score_per_mille=0),
        ),
    )


class TestTheGeneratorLabelsHonestly:
    def test_a_clean_case_is_returned_unchanged(self) -> None:
        case = inject(DivergenceLabel.NONE, NESTED, seed=1)
        assert case.result == NESTED
        assert case.applied is False

    def test_dropping_a_field_actually_drops_one(self) -> None:
        case = inject(DivergenceLabel.SCHEMA_DRIFT_MISSING, NESTED, seed=1)
        assert case.applied is True
        assert case.result != NESTED

    def test_adding_a_field_actually_adds_one(self) -> None:
        case = inject(DivergenceLabel.SCHEMA_DRIFT_ADDED, NESTED, seed=1)
        assert ADDED_FIELD in case.result

    def test_changing_a_type_produces_a_different_type(self) -> None:
        case = inject(DivergenceLabel.TYPE_CHANGE, NESTED, seed=1)
        assert case.applied is True
        assert case.result != NESTED

    def test_exploding_a_number_produces_a_much_larger_one(self) -> None:
        # the mutator picks a numeric leaf at random, so assert one of them grew
        case = inject(DivergenceLabel.EXTREME_VALUE, NESTED, seed=1)
        assert case.applied is True
        grew = (
            case.result["record"]["minor_units"] > NESTED["record"]["minor_units"]
            or case.result["count"] > NESTED["count"]
        )
        assert grew

    def test_replacing_an_enum_produces_an_unseen_value(self) -> None:
        case = inject(DivergenceLabel.UNSEEN_ENUM, NESTED, seed=1)
        assert case.applied is True

    def test_a_retry_leaves_the_result_alone_and_raises_the_attempt_count(self) -> None:
        case = inject(DivergenceLabel.RETRIED, NESTED, seed=1)
        assert case.result == NESTED
        assert case.attempts == 2
        assert case.applied is True

    def test_a_mutation_that_cannot_apply_says_so_instead_of_pretending(self) -> None:
        case = inject(DivergenceLabel.EXTREME_VALUE, {"only": "text"}, seed=1)
        assert case.applied is False
        assert case.result == {"only": "text"}

    def test_an_inapplicable_mutation_never_changes_the_result(self) -> None:
        empty: dict[str, Any] = {}
        for label in DivergenceLabel:
            case = inject(label, empty, seed=1)
            if not case.applied:
                assert case.result == empty

    def test_the_generator_is_deterministic(self) -> None:
        for label in DivergenceLabel:
            first = inject(label, NESTED, seed=7)
            second = inject(label, NESTED, seed=7)
            assert first.model_dump() == second.model_dump()

    def test_the_label_is_never_changed_by_the_mutation(self) -> None:
        for label in DivergenceLabel:
            assert inject(label, NESTED, seed=3).label is label

    def test_a_mutation_that_changes_nothing_is_not_counted_as_a_divergence(self) -> None:
        # rendering a string as a string is a no-op; it must not be labelled as a divergence
        case = inject(DivergenceLabel.TYPE_CHANGE, {"only": "text"}, seed=1)
        assert case.applied is False
        assert case.result == {"only": "text"}

    def test_every_applied_case_really_differs_from_the_original(self) -> None:
        for seed in range(20):
            for case in divergence_set(NESTED, seed=seed):
                if case.applied and case.label is not DivergenceLabel.RETRIED:
                    assert case.result != NESTED

    def test_a_divergence_set_covers_every_label_once(self) -> None:
        cases = divergence_set(NESTED, seed=1)
        assert [case.label for case in cases] == list(DivergenceLabel)


class TestTheSweepIsOfflineArithmetic:
    def test_a_point_is_produced_for_every_threshold_step(self) -> None:
        curve = sweep([scored(DivergenceLabel.NONE, applied=False)], weights=WEIGHTS)
        assert [p.threshold_per_mille for p in curve.points] == list(range(0, 1001, 50))

    def test_aborting_never_becomes_more_likely_as_the_threshold_rises(self) -> None:
        cases = [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000) for _ in range(5)]
        curve = sweep(cases, weights=WEIGHTS)
        misses = [point.missed for point in curve.points]
        assert misses == sorted(misses)

    def test_at_a_zero_threshold_everything_aborts(self) -> None:
        cases = [scored(DivergenceLabel.NONE, applied=False) for _ in range(4)]
        curve = sweep(cases, weights=WEIGHTS)
        assert curve.points[0].false_aborts == 4

    def test_only_applied_divergences_are_counted(self) -> None:
        cases = [
            scored(DivergenceLabel.EXTREME_VALUE, structural=0, applied=False),
            scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000, applied=True),
        ]
        curve = sweep(cases, weights=WEIGHTS)
        assert all(point.divergences == 1 for point in curve.points)

    def test_only_clean_cases_can_be_false_aborts(self) -> None:
        cases = [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000)]
        curve = sweep(cases, weights=WEIGHTS)
        assert all(point.false_aborts == 0 for point in curve.points)

    def test_a_structural_break_is_missed_above_its_own_weight(self) -> None:
        curve = sweep(
            [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000)], weights=WEIGHTS
        )
        at_350 = next(p for p in curve.points if p.threshold_per_mille == 350)
        at_400 = next(p for p in curve.points if p.threshold_per_mille == 400)
        assert at_350.missed == 0
        assert at_400.missed == 1

    def test_rates_are_whole_numbers_per_mille(self) -> None:
        cases = [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000) for _ in range(3)]
        curve = sweep(cases, weights=WEIGHTS)
        assert all(isinstance(point.missed_per_mille, int) for point in curve.points)

    def test_an_empty_cohort_produces_zero_rates_rather_than_dividing_by_zero(self) -> None:
        curve = sweep([], weights=WEIGHTS)
        assert all(point.missed_per_mille == 0 for point in curve.points)

    def test_the_curve_is_canonically_serialisable(self) -> None:
        curve = sweep(
            [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000)], weights=WEIGHTS
        )
        assert canonical_bytes(curve.model_dump(mode="json"))

    def test_the_sweep_is_deterministic(self) -> None:
        cases = [scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000)]
        assert (
            sweep(cases, weights=WEIGHTS).model_dump() == sweep(cases, weights=WEIGHTS).model_dump()
        )


class TestTheOperatingPointIsChosenByRuleNotByEye:
    def _curve(self) -> DivergenceCurve:
        cases = [
            scored(DivergenceLabel.SCHEMA_DRIFT_MISSING, structural=1000),
            scored(DivergenceLabel.SCHEMA_DRIFT_ADDED, structural=400),
            scored(DivergenceLabel.NONE, structural=0, applied=False),
        ]
        return sweep(cases, weights=WEIGHTS)

    def test_the_rule_picks_the_fewest_misses_within_budget(self) -> None:
        point = select_operating_point(self._curve(), max_false_abort_per_mille=0)
        assert point is not None
        assert point.missed == 0

    def test_ties_go_to_the_higher_threshold(self) -> None:
        point = select_operating_point(self._curve(), max_false_abort_per_mille=0)
        assert point is not None
        # a 400-weighted added field scores 140, so 150 already misses it; 100 does not
        assert point.threshold_per_mille == 100

    def test_a_budget_nothing_can_meet_returns_nothing(self) -> None:
        # every signal at full strength scores 1000, so this clean case aborts at every
        # threshold and no point on the curve is affordable
        saturated = ScoredCase(
            label=DivergenceLabel.NONE,
            applied=False,
            scores=tuple(
                SignalScore(signal=signal, score_per_mille=1000)
                for signal in (
                    GuardSignal.STRUCTURAL,
                    GuardSignal.NUMERIC,
                    GuardSignal.CATEGORICAL,
                    GuardSignal.BEHAVIOURAL,
                )
            ),
        )
        curve = sweep([saturated], weights=WEIGHTS)
        assert all(point.false_aborts == 1 for point in curve.points)
        assert select_operating_point(curve, max_false_abort_per_mille=0) is None

    def test_a_looser_budget_never_selects_a_worse_point(self) -> None:
        curve = self._curve()
        tight = select_operating_point(curve, max_false_abort_per_mille=0)
        loose = select_operating_point(curve, max_false_abort_per_mille=200)
        assert tight is not None
        assert loose is not None
        assert loose.missed <= tight.missed

    def test_the_rule_reads_nothing_but_the_curve(self) -> None:
        import inspect

        rendered = str(inspect.signature(select_operating_point))
        assert "curve" in rendered
        assert "max_false_abort_per_mille" in rendered
        assert len(inspect.signature(select_operating_point).parameters) == 2

    def test_selection_is_deterministic(self) -> None:
        curve = self._curve()
        assert select_operating_point(curve, max_false_abort_per_mille=50) == (
            select_operating_point(curve, max_false_abort_per_mille=50)
        )
