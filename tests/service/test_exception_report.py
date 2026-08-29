"""The exception list: which cases Rote did not resolve, and why.

The track this is built for asks for the exceptions themselves, not a count. Building the list
must cost nothing, so it is derived from the evidence and from decisions already taken - never
by asking a model about five hundred cases.
"""

from __future__ import annotations

from rote.contracts.classifier import ClassificationRequest, ClassificationResponse
from rote.contracts.routing import RouteReason
from rote.service.scenario import Decision, compiled_system, demo_dataset
from rote.service.session import SessionRuntime


class Counting:
    """A model that records how often it was asked anything at all."""

    model_id = "stand-in:counting"
    prompt_template_id = "test-v1"
    is_local = False

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        self.calls += 1
        return ClassificationResponse(category="timing_cutoff", confidence_per_mille=900)


def runtime(model: object | None = None) -> SessionRuntime:
    return SessionRuntime(
        system=compiled_system(),
        dataset=demo_dataset(),
        classifier_model=model,  # type: ignore[arg-type]
    )


class TestTheReportIsFreeToBuild:
    def test_it_asks_no_model_for_the_whole_backlog(self) -> None:
        model = Counting()
        session = runtime(model)
        report = session.exception_report()
        assert len(report) > 0
        assert model.calls == 0, "building the exception list must not cost a model call"

    def test_it_covers_every_case_that_is_not_automatable(self) -> None:
        session = runtime()
        report = session.exception_report()
        eligible = [i for i in session.backlog() if session.triage(i.exception_id).eligible]
        assert len(report) == len(session.backlog()) - len(eligible)

    def test_it_can_also_return_the_whole_backlog(self) -> None:
        session = runtime()
        assert len(session.exception_report(unresolved_only=False)) == len(session.backlog())


class TestEveryRowExplainsItself:
    def test_each_row_names_a_reason(self) -> None:
        session = runtime()
        for row in session.exception_report()[:40]:
            assert row.reason, row.exception_id

    def test_an_ambiguous_row_names_the_competing_procedures(self) -> None:
        session = runtime()
        rows = [r for r in session.exception_report() if r.fitting_count > 1]
        assert rows, "the backlog should contain ambiguous cases"
        for row in rows[:20]:
            assert len(row.fitting_categories) == row.fitting_count
            assert row.reason == RouteReason.AMBIGUOUS_EVIDENCE.value

    def test_it_carries_the_money_so_a_reviewer_can_triage(self) -> None:
        session = runtime()
        row = session.exception_report()[0]
        assert row.internal_minor_units > 0
        assert row.internal_currency

    def test_an_unworked_row_is_marked_as_predicted_not_decided(self) -> None:
        session = runtime()
        row = session.exception_report()[0]
        assert row.worked is False
        assert row.status == "open"


class TestAResolvedCaseReportsWhatActuallyHappened:
    def test_a_refused_case_carries_its_real_route_reason(self) -> None:
        session = runtime()
        target = next(
            i.exception_id for i in session.backlog() if session.triage(i.exception_id).ambiguous
        )
        session.resolve(target)
        row = next(r for r in session.exception_report() if r.exception_id == target)
        assert row.worked is True
        assert row.status == "refused"
        assert row.reason == RouteReason.AMBIGUOUS_EVIDENCE.value
        assert row.plan_lookups == 0
        assert row.compiled_steps_executed == 0

    def test_an_automated_case_leaves_the_exception_list(self) -> None:
        session = runtime()
        target = next(
            i.exception_id for i in session.backlog() if session.triage(i.exception_id).eligible
        )
        assert session.resolve(target).decision is Decision.AUTOMATE
        assert all(r.exception_id != target for r in session.exception_report())

    def test_a_verification_refusal_is_reported_as_such(self) -> None:
        session = SessionRuntime(
            system=compiled_system(), dataset=demo_dataset(), verify_evidence=True
        )
        target = next(
            i.exception_id for i in session.backlog() if session.triage(i.exception_id).eligible
        )
        from rote.bootstrap.evidence_corruption import EvidenceError

        session.corrupt_case(target, EvidenceError.CROSS_CATEGORY)
        session.resolve(target)
        row = next(r for r in session.exception_report() if r.exception_id == target)
        assert row.reason in {
            RouteReason.EVIDENCE_MISMATCH.value,
            RouteReason.EVIDENCE_UNVERIFIABLE.value,
        }
        assert row.worked is True


class TestTheReportIsReadOnly:
    def test_building_it_changes_nothing(self) -> None:
        session = runtime()
        before_world = session.world_view().world_hash
        before_ledger = len(session.ledger.entries)
        session.exception_report()
        session.exception_report(unresolved_only=False)
        assert session.world_view().world_hash == before_world
        assert len(session.ledger.entries) == before_ledger
