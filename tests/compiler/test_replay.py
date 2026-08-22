from collections.abc import Sequence

import pytest

from rote.compiler.builder import build_plan
from rote.compiler.replay import replay_plan, validate_plan
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.plan import Plan, PolicyRequirement
from rote.contracts.trajectory import Trajectory
from tests.compiler.builders import build_with_steps

POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha", "beta"}), max_per_action={Currency.INR: 50_000}
)


def run(index: int, *, window: int = 7, second_value: str | None = None) -> Trajectory:
    line = second_value if second_value is not None else f"BNK-{index}"
    return build_with_steps(
        f"t{index}",
        [
            ("alpha", {"record_id": f"REC-{index}", "window": window}, {"line_id": line}),
            ("beta", {"line_id": line}, {"ok": 1}),
        ],
        task_input={"record_id": f"REC-{index}"},
    )


def plan_from(runs: Sequence[Trajectory]) -> Plan:
    return build_plan(
        runs,
        domain=Domain.RECONCILIATION,
        category=ExceptionCategory.FEE_MISMATCH,
        policy=POLICY,
    )


class TestReplayingASingleRun:
    def test_a_plan_reproduces_the_run_it_was_taught_from(self) -> None:
        runs = [run(i) for i in range(20)]
        outcome = replay_plan(plan_from(runs), runs[0])
        assert outcome.path_equal is True
        assert outcome.playback_miss is False

    def test_a_plan_reproduces_a_run_it_has_never_seen(self) -> None:
        taught = [run(i) for i in range(20)]
        unseen = run(999)
        outcome = replay_plan(plan_from(taught), unseen)
        assert outcome.path_equal is True

    def test_the_replay_reports_which_run_it_examined(self) -> None:
        runs = [run(i) for i in range(20)]
        outcome = replay_plan(plan_from(runs), runs[3])
        assert outcome.trajectory_id == runs[3].trajectory_id


class TestAPlaybackMissIsAFailureNotASkip:
    def test_asking_for_arguments_that_were_never_recorded_is_a_miss(self) -> None:
        taught = [run(i, window=7) for i in range(20)]
        divergent = run(999, window=99)
        outcome = replay_plan(plan_from(taught), divergent)
        assert outcome.playback_miss is True
        assert outcome.path_equal is False

    def test_a_miss_names_the_step_it_happened_at(self) -> None:
        taught = [run(i, window=7) for i in range(20)]
        outcome = replay_plan(plan_from(taught), run(999, window=99))
        assert outcome.truncated_at == 0
        assert "alpha" in outcome.detail

    def test_a_miss_never_silently_counts_as_a_pass(self) -> None:
        taught = [run(i, window=7) for i in range(20)]
        report = validate_plan(plan_from(taught), [run(999, window=99)])
        assert report.playback_misses == 1
        assert report.passed is False


class TestValidationReport:
    def test_a_clean_holdout_validates(self) -> None:
        taught = [run(i) for i in range(20)]
        holdout = [run(100 + i) for i in range(10)]
        report = validate_plan(plan_from(taught), holdout)
        assert report.holdout_size == 10
        assert report.path_equal == 10
        assert report.passed is True

    def test_a_mixed_holdout_does_not_pass(self) -> None:
        taught = [run(i, window=7) for i in range(20)]
        holdout = [run(100 + i) for i in range(9)] + [run(500, window=99)]
        report = validate_plan(plan_from(taught), holdout)
        assert report.path_equal == 9
        assert report.passed is False

    def test_an_empty_holdout_never_passes(self) -> None:
        taught = [run(i) for i in range(20)]
        report = validate_plan(plan_from(taught), [])
        assert report.holdout_size == 0
        assert report.passed is False

    def test_every_holdout_run_gets_its_own_outcome(self) -> None:
        taught = [run(i) for i in range(20)]
        holdout = [run(100 + i) for i in range(5)]
        assert len(validate_plan(plan_from(taught), holdout).outcomes) == 5

    def test_validation_is_deterministic(self) -> None:
        taught = [run(i) for i in range(20)]
        holdout = [run(100 + i) for i in range(5)]
        plan = plan_from(taught)
        first = validate_plan(plan, holdout)
        second = validate_plan(plan, holdout)
        assert first.model_dump() == second.model_dump()


class TestTruncatedPlans:
    def test_a_truncated_plan_validates_only_its_prefix(self) -> None:
        taught = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"record_id": f"REC-{i}"}, {"ok": 1}),
                    ("beta", {"derived": i * 7 + 3}, {"ok": 1}),
                ],
                task_input={"record_id": f"REC-{i}"},
            )
            for i in range(20)
        ]
        plan = plan_from(taught)
        assert plan.truncated is True
        outcome = replay_plan(plan, taught[0])
        assert outcome.path_equal is True
        assert outcome.truncated_at == 1

    def test_a_plan_with_no_steps_cannot_be_validated(self) -> None:
        taught = [
            build_with_steps(f"t{i}", [("alpha", {"n": i}, {"ok": 1})], task_input={"n": str(i)})
            for i in range(20)
        ]
        plan = plan_from(taught)
        assert plan.steps == ()
        with pytest.raises(CompilerError):
            validate_plan(plan, taught)


class TestReplayUsesNoLiveTool:
    def test_replay_never_touches_a_real_adapter(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path("rote/compiler/replay.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "domain" not in node.module
                assert "safety" not in node.module
