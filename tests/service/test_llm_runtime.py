"""The live runtime with a real-model classifier plugged in.

No network and no credentials: the model here is a stand-in for a provider adapter, so these
tests pin what the RUNTIME does with whatever a real model says or fails to say.
"""

from __future__ import annotations

import pytest

from rote.contracts.classifier import ClassificationRequest, ClassificationResponse
from rote.contracts.errors import ClassifierError
from rote.contracts.ledger import LedgerEventType
from rote.contracts.routing import RouteKind, RouteReason
from rote.service.scenario import Decision, compiled_system, demo_dataset
from rote.service.session import SessionRuntime

CLEAN = demo_dataset()


class Saying:
    """A hosted model that always answers the same way. Not local, so notes are withheld."""

    model_id = "stand-in:always"
    prompt_template_id = "test-v1"
    is_local = False

    def __init__(self, category: str, confidence: int = 900) -> None:
        self._answer = ClassificationResponse(category=category, confidence_per_mille=confidence)
        self.seen: list[ClassificationRequest] = []

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        self.seen.append(request)
        return self._answer


class Failing:
    """A hosted model whose provider is down."""

    model_id = "stand-in:broken"
    prompt_template_id = "test-v1"
    is_local = False

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        self.calls += 1
        raise self._error


def session(model: object | None = None, **kwargs: object) -> SessionRuntime:
    return SessionRuntime(
        system=compiled_system(),
        dataset=demo_dataset(),
        classifier_model=model,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def a_case(runtime: SessionRuntime) -> str:
    return runtime.backlog()[0].exception_id


# ------------------------------------------------------- 8. notes withheld by the runtime
class TestTheRuntimeWithholdsNotesFromAHostedModel:
    def test_a_hosted_model_is_handed_no_untrusted_text(self) -> None:
        model = Saying("fee_mismatch")
        runtime = session(model)
        for item in runtime.backlog()[:5]:
            runtime.preview(item.exception_id)
        assert model.seen, "the model was never asked"
        assert all(request.untrusted == () for request in model.seen)

    def test_the_withholding_is_recorded_rather_than_silent(self) -> None:
        runtime = session(Saying("fee_mismatch"))
        target = a_case(runtime)
        assert runtime.preview(target).untrusted_withheld > 0
        assert runtime.resolve(target).untrusted_withheld > 0

    def test_the_deterministic_default_is_local_and_withholds_nothing(self) -> None:
        runtime = session()
        assert runtime.classifier_is_local is True
        assert runtime.preview(a_case(runtime)).untrusted_withheld == 0

    def test_every_case_in_the_backlog_carries_a_note_so_this_matters(self) -> None:
        assert all(exception.untrusted for exception in CLEAN.exceptions)


# ------------------------------------------- 10-13. a classifier failure escalates, quietly
FAILURES = [
    TimeoutError("read timed out"),
    ClassifierError("provider returned something that is not a classification"),
    OSError("connection refused"),
    RuntimeError("unexpected provider exception"),
]


class TestAClassifierFailureBecomesAControlledRefusal:
    @pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
    def test_it_escalates_instead_of_raising(self, error: Exception) -> None:
        runtime = session(Failing(error))
        view = runtime.resolve(a_case(runtime))
        assert view.decision is Decision.ESCALATE
        assert view.route_kind is RouteKind.LIVE_AGENT
        assert view.route_reason is RouteReason.CLASSIFIER_UNAVAILABLE

    @pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
    def test_nothing_is_looked_up_executed_or_moved(self, error: Exception) -> None:
        runtime = session(Failing(error))
        before = runtime.world_view().world_hash
        for item in runtime.backlog()[:15]:
            view = runtime.resolve(item.exception_id)
            assert view.plan_lookups == 0
            assert view.compiled_steps_executed == 0
            assert view.guard_inspections == 0
            assert runtime.count_events(item.exception_id, LedgerEventType.INTENT) == 0
            assert runtime.count_events(item.exception_id, LedgerEventType.OUTCOME) == 0
        assert runtime.world_view().world_hash == before
        assert runtime.ledger_view().valid is True

    def test_the_failure_is_visible_in_the_refusal_rather_than_hidden(self) -> None:
        runtime = session(Failing(TimeoutError("read timed out")))
        view = runtime.resolve(a_case(runtime))
        assert view.route_detail
        assert "classifier" in view.headline.lower() or "model" in view.headline.lower()

    # a silent fallback would turn a production outage into an invisible behaviour change
    def test_it_does_not_fall_back_to_the_deterministic_classifier(self) -> None:
        broken = Failing(TimeoutError("read timed out"))
        runtime = session(broken)
        decisions = {runtime.resolve(i.exception_id).decision for i in runtime.backlog()[:20]}
        assert decisions == {Decision.ESCALATE}
        assert broken.calls == 20

    def test_a_preview_survives_a_broken_provider_too(self) -> None:
        runtime = session(Failing(OSError("connection refused")))
        preview = runtime.preview(a_case(runtime))
        assert preview.route_reason is RouteReason.CLASSIFIER_UNAVAILABLE
        assert preview.plan_id is None

    def test_verification_still_runs_ahead_of_the_classifier(self) -> None:
        runtime = session(Failing(TimeoutError("t")), verify_evidence=True)
        view = runtime.resolve(a_case(runtime))
        assert view.decision is Decision.ESCALATE
        assert view.compiled_steps_executed == 0


# ------------------------------------------- the model narrows, it never widens
class TestAWrongModelAnswerCannotAcquireAuthority:
    def test_a_category_the_evidence_contradicts_is_vetoed(self) -> None:
        # the model insists on one category for every case; the router checks the precondition
        runtime = session(Saying("duplicate_entry", 1000))
        reasons = {runtime.resolve(i.exception_id).route_reason for i in runtime.backlog()[:40]}
        assert RouteReason.PRECONDITION_CONTRADICTION in reasons

    def test_a_confident_wrong_answer_never_beats_ambiguity(self) -> None:
        runtime = session(Saying("fee_mismatch", 1000))
        for item in runtime.backlog()[:60]:
            view = runtime.resolve(item.exception_id)
            if view.route_reason is RouteReason.AMBIGUOUS_EVIDENCE:
                assert view.plan_lookups == 0
                assert view.compiled_steps_executed == 0
                return
        pytest.fail("no ambiguous case appeared in the first 60")

    def test_a_hallucinated_category_becomes_unknown_and_escalates(self) -> None:
        runtime = session(Saying("definitely_a_fee", 1000))
        view = runtime.resolve(a_case(runtime))
        assert view.decision is Decision.ESCALATE
        assert view.route_reason is RouteReason.UNKNOWN_CATEGORY
        assert view.plan_lookups == 0

    def test_low_confidence_escalates_however_right_the_category_is(self) -> None:
        runtime = session(Saying("fee_mismatch", 10))
        view = runtime.resolve(a_case(runtime))
        assert view.decision is Decision.ESCALATE
        assert view.route_reason is RouteReason.LOW_CONFIDENCE
        assert view.plan_lookups == 0

    # the whole claim in one test: no answer the model can give automates a case that the
    # deterministic classifier would have refused
    def test_no_model_answer_widens_what_rote_will_automate(self) -> None:
        baseline = session()
        automated_by_default = {
            item.exception_id
            for item in baseline.backlog()
            if baseline.resolve(item.exception_id).decision is Decision.AUTOMATE
        }
        for category in ("fee_mismatch", "fx_rounding", "duplicate_entry", "timing_cutoff"):
            runtime = session(Saying(category, 1000))
            automated = {
                item.exception_id
                for item in runtime.backlog()
                if runtime.resolve(item.exception_id).decision is Decision.AUTOMATE
            }
            extra = automated - automated_by_default
            assert not extra, f"{category} automated {len(extra)} cases the default refused"


# ------------------------------------------- 14. deterministic mode is untouched
class TestDeterministicModeStillReproducesV2:
    def test_the_frozen_coverage_is_unchanged(self) -> None:
        runtime = session()
        decisions = [runtime.resolve(i.exception_id).decision for i in runtime.backlog()]
        assert sum(1 for d in decisions if d is Decision.AUTOMATE) == 184
        assert sum(1 for d in decisions if d is Decision.ESCALATE) == 316

    def test_the_default_classifier_is_still_the_deterministic_one(self) -> None:
        assert session().classifier_model_id == "structured-fields-double-1"

    def test_the_new_route_reason_is_additive_only(self) -> None:
        assert RouteReason.PLAN_MATCHED.value == "plan_matched"
        assert RouteReason.AMBIGUOUS_EVIDENCE.value == "ambiguous_evidence"
        assert RouteReason.EVIDENCE_MISMATCH.value == "evidence_mismatch"
        assert RouteReason.CLASSIFIER_UNAVAILABLE.value == "classifier_unavailable"


# ------------------------------------------- the adversarial branch is research-only
class TestNothingInProductionExposesAModelToUntrustedText:
    def test_no_shipped_module_opts_into_reading_free_text(self) -> None:
        import pathlib

        root = pathlib.Path("rote")
        offenders = [
            path
            for path in root.rglob("*.py")
            if "may_read_untrusted=True" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"a shipped module exposes a model to free text: {offenders}"

    def test_the_runtime_only_ever_asks_for_the_default_construction(self) -> None:
        import pathlib

        source = pathlib.Path("rote/service/session.py").read_text(encoding="utf-8")
        assert "may_read_untrusted" not in source
