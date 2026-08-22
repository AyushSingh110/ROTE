import pytest

from rote.compiler.selection import RejectionReason, hash_split, select_eligible
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import CompilerError
from tests.compiler.builders import ALPHA, build, population


class TestOnlyVerifiedTrajectoriesAreEligible:
    def test_a_checker_verified_resolution_is_eligible(self) -> None:
        chosen, report = select_eligible([build("a", ALPHA)], domain=Domain.RECONCILIATION)
        assert len(chosen) == 1
        assert report.eligible == 1

    def test_a_failed_verdict_is_rejected(self) -> None:
        chosen, report = select_eligible(
            [build("a", ALPHA, verdict=CheckerVerdict.FAIL)], domain=Domain.RECONCILIATION
        )
        assert chosen == ()
        assert report.rejected[RejectionReason.NOT_VERIFIED] == 1

    def test_an_undetermined_verdict_is_rejected(self) -> None:
        _chosen, report = select_eligible(
            [build("a", ALPHA, verdict=CheckerVerdict.UNDETERMINED)],
            domain=Domain.RECONCILIATION,
        )
        assert report.rejected[RejectionReason.NOT_VERIFIED] == 1

    def test_an_unlabelled_trajectory_is_rejected(self) -> None:
        _chosen, report = select_eligible(
            [build("a", ALPHA, verdict=None)], domain=Domain.RECONCILIATION
        )
        assert report.rejected[RejectionReason.UNLABELLED] == 1

    def test_an_escalated_run_is_rejected_even_if_verified(self) -> None:
        _chosen, report = select_eligible(
            [build("a", ALPHA, outcome="escalated", verdict=CheckerVerdict.PASS)],
            domain=Domain.RECONCILIATION,
        )
        assert report.rejected[RejectionReason.NOT_RESOLVED] == 1

    def test_another_domain_is_rejected(self) -> None:
        _chosen, report = select_eligible(
            [build("a", ALPHA, domain=Domain.DISPUTE_EVIDENCE)], domain=Domain.RECONCILIATION
        )
        assert report.rejected[RejectionReason.WRONG_DOMAIN] == 1

    def test_a_run_with_no_steps_is_rejected(self) -> None:
        _chosen, report = select_eligible([build("a", ())], domain=Domain.RECONCILIATION)
        assert report.rejected[RejectionReason.NO_STEPS] == 1

    def test_every_trajectory_is_either_eligible_or_counted_as_rejected(self) -> None:
        mixed = [
            build("ok", ALPHA),
            build("bad", ALPHA, verdict=CheckerVerdict.FAIL),
            build("raw", ALPHA, verdict=None),
            build("empty", ()),
        ]
        _chosen, report = select_eligible(mixed, domain=Domain.RECONCILIATION)
        assert report.eligible + sum(report.rejected.values()) == len(mixed)

    def test_the_report_names_the_examined_total(self) -> None:
        _chosen, report = select_eligible(
            [build(f"t{i}", ALPHA) for i in range(5)], domain=Domain.RECONCILIATION
        )
        assert report.examined == 5

    def test_selection_preserves_order(self) -> None:
        built = [build(f"t{i}", ALPHA) for i in range(5)]
        chosen, _report = select_eligible(built, domain=Domain.RECONCILIATION)
        assert [t.correlation_id for t in chosen] == [t.correlation_id for t in built]


class TestTheHoldoutSplitIsStable:
    def test_the_same_input_splits_the_same_way_every_time(self) -> None:
        built = population(ALPHA, 200)
        first = hash_split(built, holdout_fraction=0.3)
        second = hash_split(built, holdout_fraction=0.3)
        assert [t.correlation_id for t in first.fit] == [t.correlation_id for t in second.fit]

    def test_the_split_does_not_depend_on_input_order(self) -> None:
        built = population(ALPHA, 200)
        forward = hash_split(built, holdout_fraction=0.3)
        backward = hash_split(list(reversed(built)), holdout_fraction=0.3)
        assert {t.correlation_id for t in forward.holdout} == {
            t.correlation_id for t in backward.holdout
        }

    def test_fit_and_holdout_are_disjoint_and_complete(self) -> None:
        built = population(ALPHA, 200)
        split = hash_split(built, holdout_fraction=0.3)
        fit_ids = {t.trajectory_id for t in split.fit}
        holdout_ids = {t.trajectory_id for t in split.holdout}
        assert fit_ids & holdout_ids == set()
        assert len(fit_ids | holdout_ids) == 200

    def test_the_holdout_is_roughly_the_requested_fraction(self) -> None:
        split = hash_split(population(ALPHA, 400), holdout_fraction=0.3)
        assert 0.22 <= len(split.holdout) / 400 <= 0.38

    def test_a_holdout_of_zero_keeps_everything_for_fitting(self) -> None:
        split = hash_split(population(ALPHA, 50), holdout_fraction=0.0)
        assert split.holdout == ()
        assert len(split.fit) == 50

    def test_an_impossible_fraction_is_rejected(self) -> None:
        with pytest.raises(CompilerError):
            hash_split(population(ALPHA, 10), holdout_fraction=1.5)
