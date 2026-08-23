import pytest

from rote.contracts.common import Currency, ExceptionCategory, Money
from rote.contracts.reconciliation import FeeSchedule
from rote.eval.fee_rule import Rounding, discriminate, expected_fee

INR = Currency.INR


def schedule(flat: int = 0, bps: int = 0, currency: Currency = INR) -> FeeSchedule:
    return FeeSchedule(
        merchant_id="M-1", currency=currency, flat_fee_minor_units=flat, percentage_bps=bps
    )


def money(minor: int, currency: Currency = INR) -> Money:
    return Money(minor_units=minor, currency=currency)


class TestTheExpectedFeeIsIntegerArithmetic:
    def test_a_flat_fee_alone(self) -> None:
        assert expected_fee(100_000, schedule(flat=500)) == 500

    def test_a_percentage_alone(self) -> None:
        assert expected_fee(100_000, schedule(bps=250)) == 2_500

    def test_both_parts_add(self) -> None:
        assert expected_fee(100_000, schedule(flat=500, bps=250)) == 3_000

    # 33_333 * 250 / 10_000 is 833.325; minor units cannot be fractional
    def test_the_percentage_floors_by_default(self) -> None:
        assert expected_fee(33_333, schedule(bps=250)) == 833

    def test_half_up_is_available_so_the_rounding_choice_can_be_measured(self) -> None:
        assert expected_fee(33_333, schedule(bps=250), rounding=Rounding.HALF_UP) == 833
        assert expected_fee(30_000, schedule(bps=15), rounding=Rounding.HALF_UP) == 45
        assert expected_fee(30_000, schedule(bps=15), rounding=Rounding.FLOOR) == 45

    def test_a_half_case_separates_the_two_roundings(self) -> None:
        assert expected_fee(10_000, schedule(bps=5), rounding=Rounding.FLOOR) == 5
        assert expected_fee(1_000, schedule(bps=5)) == 0
        assert expected_fee(1_000, schedule(bps=5), rounding=Rounding.HALF_UP) == 1

    def test_the_answer_is_always_an_integer(self) -> None:
        value = expected_fee(99_999, schedule(flat=17, bps=137))
        assert isinstance(value, int) and not isinstance(value, bool)


class TestTheRuleAsPreRegistered:
    def test_a_gap_equal_to_the_expected_fee_is_a_fee_mismatch(self) -> None:
        verdict = discriminate(money(100_000), money(97_000), schedule(flat=3_000))
        assert verdict is ExceptionCategory.FEE_MISMATCH

    def test_a_gap_that_is_not_the_expected_fee_is_a_partial_payment(self) -> None:
        verdict = discriminate(money(100_000), money(50_000), schedule(flat=3_000))
        assert verdict is ExceptionCategory.PARTIAL_PAYMENT

    def test_the_rule_only_ever_names_these_two_categories(self) -> None:
        outcomes = {
            discriminate(money(100_000), money(100_000 - gap), schedule(flat=3_000))
            for gap in (1, 2_999, 3_000, 3_001, 99_999)
        }
        assert outcomes <= {ExceptionCategory.FEE_MISMATCH, ExceptionCategory.PARTIAL_PAYMENT}


class TestWhereTheRuleDoesNotApply:
    # the rule is written for a shortfall; without one it must abstain, not guess
    @pytest.mark.parametrize("bank", [100_000, 120_000])
    def test_it_abstains_when_the_bank_did_not_pay_less(self, bank: int) -> None:
        assert discriminate(money(100_000), money(bank), schedule(flat=500)) is None

    def test_it_abstains_when_the_currencies_differ(self) -> None:
        assert discriminate(money(100_000), money(97_000, Currency.USD), schedule()) is None

    def test_it_abstains_when_the_schedule_is_for_another_currency(self) -> None:
        assert discriminate(money(100_000), money(97_000), schedule(currency=Currency.USD)) is None

    def test_it_abstains_when_there_is_no_bank_amount(self) -> None:
        assert discriminate(money(100_000), None, schedule(flat=500)) is None

    def test_it_abstains_when_there_is_no_schedule(self) -> None:
        assert discriminate(money(100_000), money(97_000), None) is None
