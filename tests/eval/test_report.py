import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from rote.contracts.checker import CheckerVerdict
from rote.contracts.evaluation import (
    EvalPath,
    RepeatRecord,
    ReplayRecord,
    RunRecord,
    TerminalState,
)
from rote.eval.report import (
    accuracy,
    consistency,
    cost,
    deterministic_resolution,
    escalations,
    replay_fidelity,
)
from rote.eval.runlog import read_records, write_records

EVAL_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"


def run(
    task: str,
    path: EvalPath = EvalPath.ROTE,
    *,
    terminal: TerminalState = TerminalState.RESOLVED_COMPILED,
    verdict: CheckerVerdict = CheckerVerdict.PASS,
    post_calls: int = 0,
    escalation: str | None = None,
    **overrides: Any,
) -> RunRecord:
    fields: dict[str, Any] = {
        "correlation_id": f"{task}:{path.value}",
        "task_id": task,
        "seed": 5,
        "path": path,
        "terminal_state": terminal,
        "llm_calls_classification": 1,
        "llm_calls_post_classification": post_calls,
        "route_kind": "compiled_plan",
        "route_reason": "plan_matched",
        "escalation_reason": escalation,
        "plan_id": "reconciliation:fee_mismatch",
        "plan_version": 1,
        "checker_verdict": verdict,
        "checker_version": "reconciliation-1",
        "agent_model_id": "offline-heuristic-1",
        "outcome_hash": "a" * 64,
        "tokens_in": 0,
        "tokens_out": 0,
        "wall_ms_total": 10,
        "wall_ms_excluding_tool_io": 4,
        "steps": 3,
    }
    fields.update(overrides)
    return RunRecord.model_validate(fields)


def repeat(
    task: str, index: int, digest: str, *, path: EvalPath = EvalPath.ROTE, slots: int = 0
) -> RepeatRecord:
    return RepeatRecord(
        task_id=task,
        path=path,
        repeat_index=index,
        outcome_hash=digest,
        plan_id="reconciliation:fee_mismatch",
        slot_call_count=slots,
    )


class TestTheRunLogIsTheOnlySourceOfNumbers:
    def test_records_survive_a_round_trip_through_the_file(self, tmp_path: Any) -> None:
        path = tmp_path / "runs.jsonl"
        original = [run("t1"), run("t2", EvalPath.LIVE_AGENT)]
        write_records(path, original)
        assert read_records(path, RunRecord) == original

    def test_one_line_holds_one_record(self, tmp_path: Any) -> None:
        path = tmp_path / "runs.jsonl"
        write_records(path, [run("t1"), run("t2")])
        assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2

    # a log that cannot be validated is not evidence, so reading it fails loudly
    def test_a_malformed_line_is_refused_rather_than_skipped(self, tmp_path: Any) -> None:
        path = tmp_path / "runs.jsonl"
        write_records(path, [run("t1")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"task_id": "t2"}\n')
        with pytest.raises(ValidationError):
            read_records(path, RunRecord)

    def test_an_unknown_field_is_refused(self, tmp_path: Any) -> None:
        path = tmp_path / "runs.jsonl"
        write_records(path, [run("t1")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(run("t3").model_dump_json()[:-1] + ',"extra": 1}\n')
        with pytest.raises(ValidationError):
            read_records(path, RunRecord)

    def test_the_evaluator_is_never_imported_by_the_system_it_measures(self) -> None:
        root = EVAL_PACKAGE.parent
        for package in ("runtime", "agent", "compiler", "safety", "recorder", "contracts"):
            for source in sorted((root / package).rglob("*.py")):
                assert "rote.eval" not in source.read_text(encoding="utf-8")


class TestDeterministicResolution:
    def test_only_a_compiled_resolution_counts(self) -> None:
        summary = deterministic_resolution(
            [
                run("t1"),
                run("t2", terminal=TerminalState.RESOLVED_LIVE, post_calls=4),
                run("t3", terminal=TerminalState.ESCALATED, escalation="no_plan"),
            ]
        )
        assert summary.total == 3
        assert summary.resolved_deterministically == 1
        assert summary.rate_per_mille == 333

    # the claim is one bounded classification call, not zero calls
    def test_a_compiled_resolution_still_costs_one_classification_call(self) -> None:
        summary = deterministic_resolution([run("t1"), run("t2")])
        assert summary.classification_calls == 2
        assert summary.post_classification_calls == 0

    def test_a_resolution_that_took_a_model_call_is_not_deterministic(self) -> None:
        summary = deterministic_resolution(
            [run("t1", terminal=TerminalState.RESOLVED_COMPILED, post_calls=1)]
        )
        assert summary.resolved_deterministically == 0

    def test_an_empty_log_reports_zero_rather_than_dividing_by_zero(self) -> None:
        assert deterministic_resolution([]).rate_per_mille == 0


class TestConsistency:
    def test_one_distinct_hash_across_every_repeat_is_consistent(self) -> None:
        cohorts = consistency([repeat("t1", i, "same") for i in range(20)])
        assert len(cohorts) == 1
        assert cohorts[0].exceptions == 1
        assert cohorts[0].single_outcome == 1
        assert cohorts[0].rate_per_mille == 1000

    def test_a_single_differing_repeat_breaks_consistency(self) -> None:
        runs = [repeat("t1", i, "same") for i in range(19)] + [repeat("t1", 19, "other")]
        cohorts = consistency(runs)
        assert cohorts[0].single_outcome == 0
        assert cohorts[0].max_distinct == 2

    # the three cohorts must never be blurred together into one number
    def test_slot_bearing_plans_are_reported_apart_from_slot_free_ones(self) -> None:
        runs = [repeat("t1", i, "same") for i in range(3)]
        runs += [repeat("t2", i, f"h{i}", slots=1) for i in range(3)]
        labels = {cohort.label for cohort in consistency(runs)}
        assert labels == {"compiled, slot-free", "compiled, contains a slot"}

    def test_the_live_agent_is_its_own_cohort(self) -> None:
        runs = [repeat("t1", i, "same") for i in range(3)]
        runs += [repeat("t2", i, "same", path=EvalPath.LIVE_AGENT) for i in range(3)]
        labels = [cohort.label for cohort in consistency(runs)]
        assert "live agent" in labels

    def test_a_cohort_with_no_runs_is_not_invented(self) -> None:
        assert [c.label for c in consistency([repeat("t1", 0, "same")])] == ["compiled, slot-free"]


class TestEscalationsAreAlwaysExplained:
    def test_every_escalation_is_counted_under_its_reason(self) -> None:
        counts = escalations(
            [
                run("t1", terminal=TerminalState.ESCALATED, escalation="no_plan"),
                run("t2", terminal=TerminalState.ESCALATED, escalation="no_plan"),
                run("t3", terminal=TerminalState.ESCALATED, escalation="invariant_veto"),
                run("t4"),
            ]
        )
        assert [(c.reason, c.count) for c in counts] == [("no_plan", 2), ("invariant_veto", 1)]

    def test_an_escalation_without_a_reason_is_refused_at_the_boundary(self) -> None:
        with pytest.raises(ValidationError, match="reason"):
            run("t1", terminal=TerminalState.ESCALATED, escalation=None)

    def test_a_resolution_may_not_carry_an_escalation_reason(self) -> None:
        with pytest.raises(ValidationError, match="reason"):
            run("t1", escalation="no_plan")


class TestAccuracyAgainstTheChecker:
    def test_the_two_by_two_names_where_each_path_won(self) -> None:
        records = [
            run("t1", EvalPath.ROTE),
            run("t1", EvalPath.LIVE_AGENT),
            run("t2", EvalPath.ROTE, verdict=CheckerVerdict.FAIL),
            run("t2", EvalPath.LIVE_AGENT, verdict=CheckerVerdict.FAIL),
            run("t3", EvalPath.ROTE),
            run("t3", EvalPath.LIVE_AGENT, verdict=CheckerVerdict.FAIL),
            run("t4", EvalPath.ROTE, verdict=CheckerVerdict.FAIL),
            run("t4", EvalPath.LIVE_AGENT),
        ]
        report = accuracy(records)
        assert (report.both_pass, report.both_fail) == (1, 1)
        assert (report.only_rote, report.only_agent) == (1, 1)
        assert report.rote_passed == 2
        assert report.agent_passed == 2

    def test_a_task_missing_from_one_path_is_refused_rather_than_dropped(self) -> None:
        with pytest.raises(ValueError, match="both paths"):
            accuracy([run("t1", EvalPath.ROTE)])

    # escalation is a safe ending, so it must never be counted as a wrong answer
    def test_an_undetermined_verdict_counts_as_neither_pass_nor_fail(self) -> None:
        records = [
            run("t1", EvalPath.ROTE, verdict=CheckerVerdict.UNDETERMINED),
            run("t1", EvalPath.LIVE_AGENT, verdict=CheckerVerdict.UNDETERMINED),
        ]
        report = accuracy(records)
        assert (report.both_pass, report.both_fail, report.only_rote, report.only_agent) == (
            0,
            0,
            0,
            0,
        )
        assert report.undetermined == 2


class TestAuditReplayFidelity:
    def test_a_matching_replay_counts_and_a_differing_one_does_not(self) -> None:
        summary = replay_fidelity(
            [
                ReplayRecord(
                    task_id="t1",
                    original_outcome_hash="a",
                    replay_outcome_hash="a",
                    idempotency_keys_match=True,
                    first_differing_seq=None,
                ),
                ReplayRecord(
                    task_id="t2",
                    original_outcome_hash="a",
                    replay_outcome_hash="b",
                    idempotency_keys_match=False,
                    first_differing_seq=4,
                ),
            ]
        )
        assert summary.replayed == 2
        assert summary.reproduced == 1
        assert summary.keys_reproduced == 1
        assert summary.rate_per_mille == 500

    def test_a_record_claiming_a_match_with_differing_hashes_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="match"):
            ReplayRecord(
                task_id="t1",
                original_outcome_hash="a",
                replay_outcome_hash="b",
                idempotency_keys_match=True,
                first_differing_seq=None,
                match=True,
            )


class TestCostIsReportedPerPathAndNeverBlended:
    def test_each_path_gets_its_own_row(self) -> None:
        records = [run("t1"), run("t2", EvalPath.LIVE_AGENT, post_calls=6)]
        rows = {row.path: row for row in cost(records)}
        assert rows[EvalPath.ROTE].median_llm_calls == 1
        assert rows[EvalPath.LIVE_AGENT].median_llm_calls == 7

    def test_the_percentile_is_taken_from_the_honest_wall_clock(self) -> None:
        records = [
            run(f"t{i}", wall_ms_total=1000, wall_ms_excluding_tool_io=i) for i in range(1, 21)
        ]
        row = cost(records)[0]
        assert row.median_wall_ms == 10
        assert row.p95_wall_ms == 19

    def test_a_single_run_reports_itself_as_both_median_and_p95(self) -> None:
        row = cost([run("t1", wall_ms_excluding_tool_io=7)])[0]
        assert (row.median_wall_ms, row.p95_wall_ms) == (7, 7)
