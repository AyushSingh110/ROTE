import ast
import pathlib

import pytest

from rote.eval.fee_rule import Rounding
from rote.eval.stability import (
    COMFORTABLE_MULTIPLE,
    ROUNDING_BAND,
    SEED_SPREAD_FACTOR,
    Stability,
    SweepCell,
    classify,
    trend,
)

EXPERIMENT_MODULES = ("fee_rule.py", "margin.py", "stability.py")
EVAL_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
PRODUCTION = ("rote.compiler", "rote.runtime", "rote.agent", "rote.safety", "rote.recorder")


def cell(
    size: int,
    seed: int,
    *,
    min_partial: int,
    max_fee: int = 0,
    rounding: Rounding = Rounding.FLOOR,
) -> SweepCell:
    return SweepCell(
        size=size,
        seed=seed,
        rounding=rounding,
        fee_count=100,
        partial_count=50,
        max_fee_distance=max_fee,
        min_partial_distance=min_partial,
        overlaps=max_fee >= min_partial,
        margin=None if max_fee >= min_partial else min_partial - max_fee,
    )


def steady(sizes: tuple[int, ...] = (500, 1500, 5000), value: int = 900) -> list[SweepCell]:
    return [cell(size, seed, min_partial=value) for size in sizes for seed in (1, 2, 3)]


class TestTheDeclaredConstants:
    # the rounding band is the measured effect of the convention, not a chosen tolerance
    def test_the_band_is_one_minor_unit(self) -> None:
        assert ROUNDING_BAND == 1

    def test_comfortable_means_an_order_of_magnitude_above_the_band(self) -> None:
        assert COMFORTABLE_MULTIPLE == 10
        assert SEED_SPREAD_FACTOR == 10


class TestTheTrendAggregatesTheWorstCase:
    def test_it_reports_one_point_per_size_and_rounding(self) -> None:
        points = trend(steady())
        assert [point.size for point in points] == [500, 1500, 5000]

    def test_the_worst_seed_is_the_one_that_counts(self) -> None:
        cells = [
            cell(500, 1, min_partial=900),
            cell(500, 2, min_partial=40),
            cell(500, 3, min_partial=700),
        ]
        point = trend(cells)[0]
        assert point.worst_min_partial == 40
        assert point.best_min_partial == 900

    def test_the_highest_fee_distance_across_seeds_is_carried(self) -> None:
        cells = [cell(500, 1, min_partial=900, max_fee=0), cell(500, 2, min_partial=900, max_fee=1)]
        assert trend(cells)[0].worst_max_fee == 1

    def test_the_margin_is_taken_from_the_worst_pair_not_averaged(self) -> None:
        cells = [cell(500, 1, min_partial=900, max_fee=0), cell(500, 2, min_partial=40, max_fee=1)]
        assert trend(cells)[0].worst_margin == 39

    def test_the_two_roundings_are_never_mixed(self) -> None:
        cells = [
            cell(500, 1, min_partial=900, rounding=Rounding.FLOOR),
            cell(500, 1, min_partial=5, rounding=Rounding.HALF_UP),
        ]
        assert {point.rounding for point in trend(cells)} == set(Rounding)
        assert len(trend(cells)) == 2


class TestTheVerdictWasDefinedBeforeTheData:
    def test_a_steady_comfortable_minimum_is_stable(self) -> None:
        assert classify(trend(steady())) is Stability.STABLE

    def test_an_overlap_anywhere_is_unstable(self) -> None:
        cells = [*steady(), cell(5000, 4, min_partial=3, max_fee=5)]
        assert classify(trend(cells)) is Stability.UNSTABLE

    def test_reaching_the_rounding_band_is_shrinking(self) -> None:
        cells = [*steady(), cell(5000, 4, min_partial=ROUNDING_BAND)]
        assert classify(trend(cells)) is Stability.SHRINKING

    def test_an_order_of_magnitude_of_seed_spread_is_unstable(self) -> None:
        cells = [
            cell(500, 1, min_partial=900),
            cell(500, 2, min_partial=90),
            cell(1500, 1, min_partial=900),
            cell(5000, 1, min_partial=900),
        ]
        assert classify(trend(cells)) is Stability.UNSTABLE

    def test_a_minimum_that_falls_with_sample_size_is_shrinking(self) -> None:
        cells = [
            cell(500, 1, min_partial=900),
            cell(1500, 1, min_partial=400),
            cell(5000, 1, min_partial=200),
        ]
        assert classify(trend(cells)) is Stability.SHRINKING

    def test_a_steady_but_uncomfortable_minimum_is_unstable(self) -> None:
        assert classify(trend(steady(value=COMFORTABLE_MULTIPLE * ROUNDING_BAND))) is (
            Stability.UNSTABLE
        )

    def test_it_refuses_to_judge_nothing(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            classify(())


class TestTheExperimentStaysInsideItsBoundary:
    def test_no_experiment_module_imports_production_code(self) -> None:
        for name in EXPERIMENT_MODULES:
            tree = ast.parse((EVAL_PACKAGE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(PRODUCTION), f"{name} imports {node.module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith(PRODUCTION), f"{name} imports {alias.name}"
