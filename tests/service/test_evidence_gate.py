import ast
import pathlib

import pytest

from rote.contracts.common import ExceptionCategory
from rote.contracts.ledger import LedgerEventType
from rote.contracts.routing import RouteKind, RouteReason
from rote.eval.evidence_corruption import EvidenceError, corrupted_dataset
from rote.service.scenario import Decision, compiled_system, demo_dataset
from rote.service.session import SessionRuntime

ROOT = pathlib.Path(__file__).resolve().parents[2] / "rote"
CLEAN = demo_dataset()
TRUTH = {t.exception_id: t.category for t in CLEAN.ground_truths}
READ_TOOLS = {
    "get_settlement_record",
    "get_bank_line",
    "find_bank_lines_by_amount",
    "list_bank_lines_for_reference",
}


def runtime(error: EvidenceError = EvidenceError.NONE, *, verify: bool = True) -> SessionRuntime:
    data = CLEAN if error is EvidenceError.NONE else corrupted_dataset(CLEAN, error, TRUTH)
    return SessionRuntime(system=compiled_system(), dataset=data, verify_evidence=verify)


def first_where(session: SessionRuntime, reason: RouteReason) -> str:
    for item in session.backlog():
        if session.preview(item.exception_id).route_reason is reason:
            return item.exception_id
    raise LookupError(reason)


# ---------------------------------------------------------------- switch
class TestTheProbeIsBehindASwitch:
    def test_it_is_off_by_default(self) -> None:
        default = SessionRuntime(system=compiled_system(), dataset=CLEAN)
        assert default.verifies_evidence is False

    def test_the_v2_path_is_reproducible_with_it_off(self) -> None:
        session = runtime(verify=False)
        decisions = [session.resolve(i.exception_id).decision for i in session.backlog()]
        automated = sum(1 for d in decisions if d is Decision.AUTOMATE)
        assert automated == 184


# ---------------------------------------------------------------- clean
class TestCleanBehaviourIsPreserved:
    @pytest.fixture(scope="class")
    def clean(self) -> SessionRuntime:
        return runtime()

    def test_coverage_and_correctness_are_unchanged(self, clean: SessionRuntime) -> None:
        decisions = [clean.resolve(i.exception_id) for i in clean.backlog()]
        automated = [d for d in decisions if d.decision is Decision.AUTOMATE]
        assert len(automated) == 184
        assert len(decisions) - len(automated) == 316

    def test_no_clean_case_is_refused_for_a_mismatch(self, clean: SessionRuntime) -> None:
        for item in clean.backlog():
            resolution = clean.resolve(item.exception_id)
            assert resolution.route_reason is not RouteReason.EVIDENCE_MISMATCH

    def test_unambiguous_cases_still_reach_the_plan(self, clean: SessionRuntime) -> None:
        target = first_where(clean, RouteReason.PLAN_MATCHED)
        resolution = clean.resolve(target)
        assert resolution.decision is Decision.AUTOMATE
        assert resolution.plan_lookups == 1
        assert resolution.model_calls_after_classification == 0

    def test_ambiguous_cases_remain_ambiguous(self, clean: SessionRuntime) -> None:
        target = first_where(clean, RouteReason.AMBIGUOUS_EVIDENCE)
        assert clean.resolve(target).route_reason is RouteReason.AMBIGUOUS_EVIDENCE


# ---------------------------------------------------------------- corrupted
class TestCorruptedEvidenceCannotExecute:
    @pytest.mark.parametrize(
        "error",
        [
            EvidenceError.AMOUNT_OFF_BY_ONE,
            EvidenceError.REFERENCE_SUBSTITUTION,
            EvidenceError.CROSS_CATEGORY,
            EvidenceError.CANDIDATE_SUBSTITUTION,
            EvidenceError.MISSING_FIELD,
        ],
    )
    def test_nothing_is_automated_and_nothing_moves(self, error: EvidenceError) -> None:
        session = runtime(error)
        before = session.world_view().world_hash
        for item in session.backlog()[:40]:
            resolution = session.resolve(item.exception_id)
            assert resolution.decision is Decision.ESCALATE
            assert resolution.plan_lookups == 0
            assert resolution.compiled_steps_executed == 0
            assert resolution.route_reason in {
                RouteReason.EVIDENCE_MISMATCH,
                RouteReason.EVIDENCE_UNVERIFIABLE,
            }
        assert session.world_view().world_hash == before
        assert session.ledger_view().valid is True

    def test_a_missing_field_is_unverifiable_not_a_mismatch(self) -> None:
        session = runtime(EvidenceError.MISSING_FIELD)
        reasons = {session.resolve(i.exception_id).route_reason for i in session.backlog()[:30]}
        assert reasons == {RouteReason.EVIDENCE_UNVERIFIABLE}

    def test_a_candidate_only_corruption_is_unverifiable(self) -> None:
        session = runtime(EvidenceError.CANDIDATE_SUBSTITUTION)
        for item in session.backlog()[:20]:
            assert session.resolve(item.exception_id).route_reason is (
                RouteReason.EVIDENCE_UNVERIFIABLE
            )


# ---------------------------------------------------------------- safety
class TestTheSafetyBoundary:
    def test_no_financial_mutation_on_refusal(self) -> None:
        session = runtime(EvidenceError.CROSS_CATEGORY)
        before = session.world_view()
        for item in session.backlog()[:40]:
            session.resolve(item.exception_id)
        after = session.world_view()
        assert after.world_hash == before.world_hash
        assert after.adjustments == before.adjustments
        assert after.matched_records == before.matched_records

    # the verifier must not create a second authority path around the gate
    def test_the_session_never_calls_an_adapter_directly(self) -> None:
        source = (ROOT / "service" / "session.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "invoke":
                target = getattr(node.func, "value", None)
                name = getattr(target, "attr", getattr(target, "id", ""))
                assert name != "_adapters", f"direct adapter invoke at line {node.lineno}"

    def test_every_verification_read_is_recorded_by_the_gate(self) -> None:
        session = runtime()
        target = first_where(session, RouteReason.PLAN_MATCHED)
        session.resolve(target)
        tools = {
            entry.payload.get("tool")
            for entry in session.ledger.entries
            if entry.task_id == target and entry.event_type is LedgerEventType.GATE_VERDICT
        }
        assert READ_TOOLS & tools, f"no gated verification read recorded, saw {tools}"

    def test_refusal_writes_no_intent_or_outcome(self) -> None:
        session = runtime(EvidenceError.AMOUNT_OFF_BY_ONE)
        for item in session.backlog()[:20]:
            eid = item.exception_id
            session.resolve(eid)
            assert session.count_events(eid, LedgerEventType.INTENT) == 0
            assert session.count_events(eid, LedgerEventType.OUTCOME) == 0

    def test_verification_does_not_break_idempotent_replay(self) -> None:
        session = runtime()
        target = first_where(session, RouteReason.PLAN_MATCHED)
        first = session.resolve(target)
        after = session.world_view().world_hash
        second = session.resolve(target)
        assert first.decision is Decision.AUTOMATE
        assert second.already_resolved is True
        assert session.world_view().world_hash == after

    def test_verification_cannot_bypass_the_kill_switch(self) -> None:
        killed = SessionRuntime(
            system=compiled_system(killed=True), dataset=CLEAN, verify_evidence=True
        )
        target = next(
            i.exception_id
            for i in killed.backlog()
            if killed.preview(i.exception_id).route_reason is RouteReason.NO_ACTIVE_PLAN
        )
        resolution = killed.resolve(target)
        assert resolution.decision is Decision.ESCALATE
        assert resolution.compiled_steps_executed == 0
        assert resolution.route_kind is RouteKind.LIVE_AGENT


class TestTheContractAdditionIsAdditive:
    def test_the_existing_reasons_keep_their_values(self) -> None:
        assert RouteReason.PLAN_MATCHED.value == "plan_matched"
        assert RouteReason.AMBIGUOUS_EVIDENCE.value == "ambiguous_evidence"
        assert RouteReason.NO_ACTIVE_PLAN.value == "no_plan"

    def test_the_router_itself_never_emits_the_new_reasons(self) -> None:
        source = (ROOT / "runtime" / "router.py").read_text(encoding="utf-8")
        assert "EVIDENCE_MISMATCH" not in source
        assert "EVIDENCE_UNVERIFIABLE" not in source

    def test_the_new_reasons_exist_and_are_distinct(self) -> None:
        assert RouteReason.EVIDENCE_MISMATCH.value == "evidence_mismatch"
        assert RouteReason.EVIDENCE_UNVERIFIABLE.value == "evidence_unverifiable"
        assert len(set(RouteReason)) == 8


def test_the_true_category_is_never_consulted_by_the_session() -> None:
    source = (ROOT / "service" / "session.py").read_text(encoding="utf-8")
    for banned in ("ground_truth", "GroundTruth", "check_outcome", "CheckerVerdict"):
        assert banned not in source, banned
    assert ExceptionCategory.__name__ in source
