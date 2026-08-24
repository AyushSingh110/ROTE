import ast
import pathlib

import pytest

from rote.bootstrap.evidence_corruption import EvidenceError
from rote.contracts.common import GENERATED_CATEGORIES
from rote.contracts.routing import RouteReason
from rote.runtime.evidence_check import VerificationOutcome
from rote.service.scenario import Decision, compiled_system, demo_dataset
from rote.service.session import PRECONDITION_DESCRIPTIONS, SessionRuntime

ROOT = pathlib.Path(__file__).resolve().parents[2] / "rote"
CLEAN = demo_dataset()
DEMO_CORRUPTIONS = (
    EvidenceError.AMOUNT_OFF_BY_ONE,
    EvidenceError.REFERENCE_SUBSTITUTION,
    EvidenceError.CROSS_CATEGORY,
)


def sandbox() -> SessionRuntime:
    return SessionRuntime(system=compiled_system(), dataset=CLEAN)


def a_case(session: SessionRuntime, reason: RouteReason) -> str:
    for item in session.backlog():
        if session.preview(item.exception_id).route_reason is reason:
            return item.exception_id
    raise LookupError(reason)


class TestTheInvestigationViewShowsBothEvidenceSides:
    def test_it_carries_the_authoritative_comparison(self) -> None:
        session = sandbox()
        detail = session.investigation(a_case(session, RouteReason.PLAN_MATCHED))
        assert detail.verification is not None
        fields = {check.field for check in detail.verification.checks}
        assert {"internal_amount", "bank_amount", "captured_on", "merchant_id"} <= fields

    def test_a_clean_case_shows_no_mismatch(self) -> None:
        session = sandbox()
        detail = session.investigation(a_case(session, RouteReason.PLAN_MATCHED))
        assert detail.verification is not None
        assert detail.verification.mismatched_fields == ()

    def test_it_lists_every_procedure_with_whether_it_fits(self) -> None:
        session = sandbox()
        detail = session.investigation(a_case(session, RouteReason.AMBIGUOUS_EVIDENCE))
        assert len(detail.procedures) == len(GENERATED_CATEGORIES)
        assert sum(1 for p in detail.procedures if p.holds) >= 2
        for procedure in detail.procedures:
            assert procedure.precondition

    def test_every_category_has_a_readable_precondition(self) -> None:
        assert set(PRECONDITION_DESCRIPTIONS) == set(GENERATED_CATEGORIES)
        assert all(text.strip() for text in PRECONDITION_DESCRIPTIONS.values())


class TestTheCorruptionControl:
    @pytest.mark.parametrize("error", DEMO_CORRUPTIONS)
    def test_corrupting_a_case_makes_the_evidence_disagree(self, error: EvidenceError) -> None:
        session = sandbox()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        assert session.investigation(target).verification.mismatched_fields == ()  # type: ignore[union-attr]

        session.corrupt_case(target, error)
        detail = session.investigation(target)
        assert detail.corrupted_with == error.value
        assert detail.verification is not None
        assert detail.verification.outcome is not VerificationOutcome.AGREEMENT

    def test_it_only_touches_the_case_it_was_asked_about(self) -> None:
        session = sandbox()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        other = next(i.exception_id for i in session.backlog() if i.exception_id != target)
        before = session.investigation(other).facts
        session.corrupt_case(target, EvidenceError.CROSS_CATEGORY)
        assert session.investigation(other).facts == before
        assert session.investigation(other).corrupted_with is None

    def test_restoring_brings_the_original_evidence_back(self) -> None:
        session = sandbox()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        original = session.investigation(target).facts
        session.corrupt_case(target, EvidenceError.AMOUNT_OFF_BY_ONE)
        assert session.investigation(target).facts != original
        session.restore_case(target)
        assert session.investigation(target).facts == original
        assert session.investigation(target).corrupted_with is None

    def test_corruption_never_touches_the_world(self) -> None:
        session = sandbox()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        before = session.world_view().world_hash
        for error in DEMO_CORRUPTIONS:
            session.corrupt_case(target, error)
            assert session.world_view().world_hash == before
        session.restore_case(target)
        assert session.world_view().world_hash == before

    def test_an_unknown_case_is_refused(self) -> None:
        with pytest.raises(LookupError):
            sandbox().corrupt_case("EXC-nope", EvidenceError.CROSS_CATEGORY)

    def test_corrupting_clears_a_previous_decision_so_it_can_be_rerun(self) -> None:
        session = sandbox()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        session.resolve(target)
        assert session.resolve(target).already_resolved is True
        session.corrupt_case(target, EvidenceError.CROSS_CATEGORY)
        assert session.resolution_for(target) is None


class TestCorruptedEvidenceIsRefusedWithVerificationOn:
    @pytest.mark.parametrize("error", DEMO_CORRUPTIONS)
    def test_the_hero_corruption_demo(self, error: EvidenceError) -> None:
        session = SessionRuntime(system=compiled_system(), dataset=CLEAN, verify_evidence=True)
        target = a_case(session, RouteReason.PLAN_MATCHED)

        clean = session.resolve(target)
        assert clean.decision is Decision.AUTOMATE
        session.restore_case(target)

        world_before = session.world_view().world_hash
        intents_before = session.count_financial_intents(target)
        session.corrupt_case(target, error)
        refused = session.resolve(target)

        assert refused.decision is Decision.ESCALATE
        assert refused.route_reason in {
            RouteReason.EVIDENCE_MISMATCH,
            RouteReason.EVIDENCE_UNVERIFIABLE,
        }
        assert refused.plan_lookups == 0
        assert refused.compiled_steps_executed == 0
        # the refusal itself added no financial intent; the clean run before it did
        assert session.count_financial_intents(target) == intents_before
        assert session.world_view().world_hash == world_before
        assert session.ledger_view().valid is True

    def test_restoring_lets_the_same_case_automate_again(self) -> None:
        session = SessionRuntime(system=compiled_system(), dataset=CLEAN, verify_evidence=True)
        target = a_case(session, RouteReason.PLAN_MATCHED)
        session.corrupt_case(target, EvidenceError.CROSS_CATEGORY)
        assert session.resolve(target).decision is Decision.ESCALATE
        session.restore_case(target)
        assert session.resolve(target).decision is Decision.AUTOMATE


class TestResetClearsSandboxState:
    def test_reset_clears_corruption_and_the_ledger(self) -> None:
        from rote.service.session import live_session, reset_session

        session = live_session()
        target = a_case(session, RouteReason.PLAN_MATCHED)
        session.corrupt_case(target, EvidenceError.CROSS_CATEGORY)
        session.resolve(target)

        fresh = reset_session()
        assert fresh.resolution_for(target) is None
        # viewing a case performs gated authoritative reads, so it writes audit verdicts.
        # What reset must clear is the financial record and the corruption state.
        assert fresh.count_financial_intents(target) == 0
        assert fresh.investigation(target).corrupted_with is None
        assert fresh.count_financial_intents(target) == 0
        assert fresh.ledger_view().valid is True


class TestTheHttpLayerCannotBypassTheRuntime:
    def test_the_web_layer_holds_no_domain_logic(self) -> None:
        source = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for banned in (
            "precondition_holds",
            "execute_plan",
            "PolicyGate",
            "Guard(",
            "verify(",
            "ReconciliationTools",
            "corrupt(",
        ):
            assert banned not in source, f"web layer contains {banned}"

    def test_the_web_layer_never_calls_a_tool(self) -> None:
        tree = ast.parse((ROOT / "web" / "app.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name != "invoke", f"web layer invokes a tool at line {node.lineno}"

    def test_the_web_layer_imports_only_the_service_facade(self) -> None:
        tree = ast.parse((ROOT / "web" / "app.py").read_text(encoding="utf-8"))
        reached = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                reached.add(node.module)
        rote_imports = {name for name in reached if name.startswith("rote.")}
        allowed_prefixes = ("rote.service", "rote.observability", "rote.bootstrap")
        assert all(name.startswith(allowed_prefixes) for name in rote_imports), rote_imports
