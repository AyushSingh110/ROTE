import ast
import pathlib

import pytest

from rote.contracts.common import Currency, Money
from rote.contracts.reconciliation import FeeSchedule
from rote.eval.fee_rule import Rounding
from rote.eval.margin import distance, separation, summarise

EXPERIMENT_MODULES = ("fee_rule.py", "margin.py")
EVAL_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
PRODUCTION = ("rote.compiler", "rote.runtime", "rote.agent", "rote.safety", "rote.recorder")
INR = Currency.INR


def schedule(flat: int = 0, bps: int = 0, currency: Currency = INR) -> FeeSchedule:
    return FeeSchedule(
        merchant_id="M-1", currency=currency, flat_fee_minor_units=flat, percentage_bps=bps
    )


def money(minor: int, currency: Currency = INR) -> Money:
    return Money(minor_units=minor, currency=currency)


class TestTheDistance:
    def test_an_exact_fee_is_distance_zero(self) -> None:
        assert distance(money(100_000), money(97_000), schedule(flat=3_000)) == 0

    def test_the_distance_is_how_far_the_gap_is_from_the_scheduled_fee(self) -> None:
        assert distance(money(100_000), money(50_000), schedule(flat=3_000)) == 47_000

    # abs on both sides: overshooting the fee by 5 is as far as undershooting it by 5
    def test_it_is_symmetric_about_the_expected_fee(self) -> None:
        over = distance(money(100_000), money(96_995), schedule(flat=3_000))
        under = distance(money(100_000), money(97_005), schedule(flat=3_000))
        assert over == under == 5

    # the method says shortfall = abs(internal - bank), so an overpayment still has a size
    def test_a_bank_overpayment_still_yields_a_positive_shortfall(self) -> None:
        assert distance(money(100_000), money(103_000), schedule(flat=3_000)) == 0

    def test_the_two_roundings_can_differ_by_one(self) -> None:
        floor = distance(money(1_000), money(1_000), schedule(bps=5), rounding=Rounding.FLOOR)
        half = distance(money(1_000), money(1_000), schedule(bps=5), rounding=Rounding.HALF_UP)
        assert (floor, half) == (0, 1)

    @pytest.mark.parametrize(
        ("bank", "sched"),
        [
            (None, schedule(flat=500)),
            (money(97_000, Currency.USD), schedule(flat=500)),
            (money(97_000), schedule(flat=500, currency=Currency.USD)),
            (money(97_000), None),
        ],
    )
    def test_it_abstains_rather_than_guessing(self, bank: Money | None, sched: FeeSchedule) -> None:
        assert distance(money(100_000), bank, sched) is None


class TestTheDistribution:
    def test_it_reports_the_five_figures_asked_for(self) -> None:
        summary = summarise([0, 0, 1, 2, 900])
        assert summary.count == 5
        assert (summary.minimum, summary.maximum) == (0, 900)
        assert summary.at_zero == 2
        assert summary.within_one == 3
        assert summary.within_two == 4

    def test_the_median_is_a_value_that_actually_occurred(self) -> None:
        assert summarise([0, 0, 1, 900]).median == 0
        assert summarise([0, 1, 2]).median == 1

    def test_an_empty_distribution_has_no_statistics_to_report(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            summarise([])


class TestTheSeparation:
    def test_a_gap_between_the_two_ranges_is_a_margin(self) -> None:
        result = separation(fee=[0, 0, 1], partial=[900, 5_000])
        assert (result.max_fee_distance, result.min_partial_distance) == (1, 900)
        assert result.overlaps is False
        assert result.margin == 899

    def test_overlapping_ranges_have_no_margin(self) -> None:
        result = separation(fee=[0, 950], partial=[900, 5_000])
        assert result.overlaps is True
        assert result.margin is None

    # touching is not separating: a threshold would have to split equal values
    def test_ranges_that_merely_touch_still_overlap(self) -> None:
        result = separation(fee=[0, 900], partial=[900, 5_000])
        assert result.overlaps is True
        assert result.margin is None

    def test_it_refuses_to_summarise_a_missing_side(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            separation(fee=[], partial=[900])


class TestTheExperimentStaysInsideItsBoundary:
    def test_it_imports_no_production_runtime_or_compiler_code(self) -> None:
        for name in EXPERIMENT_MODULES:
            tree = ast.parse((EVAL_PACKAGE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(PRODUCTION), f"{name} imports {node.module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith(PRODUCTION), f"{name} imports {alias.name}"

    def test_the_experiment_modules_live_in_the_evaluation_package(self) -> None:
        for name in EXPERIMENT_MODULES:
            assert (EVAL_PACKAGE / name).is_file()
