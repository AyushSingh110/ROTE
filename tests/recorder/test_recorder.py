import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import RecorderError
from rote.contracts.fingerprint import structural_fingerprint
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.trajectory import GateVerdict, TrajectoryStore
from rote.domain.checkers.reconciliation import CHECKER_VERSION
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.recorder.labelling import label_trajectory
from rote.recorder.recorder import TrajectoryRecorder
from rote.recorder.store import InMemoryTrajectoryStore
from tests.domain.reference_resolver import resolve

SEED = 17
COUNT = 12


def ticking_clock() -> Iterator[datetime]:
    moment = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(milliseconds=4)


def new_recorder() -> TrajectoryRecorder:
    source = ticking_clock()
    return TrajectoryRecorder(clock=lambda: next(source))


def started_recorder(exception: ReconciliationException) -> TrajectoryRecorder:
    recorder = new_recorder()
    recorder.start(
        correlation_id=f"{exception.exception_id}:run-0",
        domain=Domain.RECONCILIATION,
        task_input_redacted=exception.facts.model_dump(mode="json"),
        untrusted_text_paths=tuple(block.source_path for block in exception.untrusted),
        agent_model_id="test-model",
        prompt_template_id="test-v1",
        dry_run=True,
    )
    return recorder


def dataset(count: int = COUNT) -> GeneratedDataset:
    return generate_dataset(seed=SEED, count=count)


class TestTheRecorderOwnsFingerprints:
    def test_the_record_step_api_accepts_no_caller_supplied_fingerprint(self):
        rendered = str(inspect.signature(TrajectoryRecorder.record_step)).lower()
        assert "fingerprint" not in rendered

    def test_the_recorder_computes_the_fingerprint_itself(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        result = {"record": {"record_id": "REC-000000", "amount": {"minor_units": 1}}}
        recorder.record_step(
            tool="get_settlement_record", args={"record_id": "REC-000000"}, result=result
        )
        trajectory = recorder.finish(outcome="resolved")
        assert trajectory.steps[0].result_fingerprint == structural_fingerprint(result)

    def test_two_results_of_the_same_shape_share_a_fingerprint(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        recorder.record_step(tool="t", args={}, result={"amount": 100})
        recorder.record_step(tool="t", args={}, result={"amount": 999})
        trajectory = recorder.finish(outcome="resolved")
        assert trajectory.steps[0].result_fingerprint == trajectory.steps[1].result_fingerprint


class TestRecorderLifecycle:
    def test_steps_are_numbered_from_zero_in_order(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        for _ in range(3):
            recorder.record_step(tool="t", args={}, result={})
        trajectory = recorder.finish(outcome="resolved")
        assert [step.index for step in trajectory.steps] == [0, 1, 2]

    def test_recording_before_start_is_rejected(self):
        with pytest.raises(RecorderError):
            new_recorder().record_step(tool="t", args={}, result={})

    def test_finishing_before_start_is_rejected(self):
        with pytest.raises(RecorderError):
            new_recorder().finish(outcome="resolved")

    def test_finishing_twice_is_rejected(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        recorder.finish(outcome="resolved")
        with pytest.raises(RecorderError):
            recorder.finish(outcome="resolved")

    def test_recording_after_finish_is_rejected(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        recorder.finish(outcome="resolved")
        with pytest.raises(RecorderError):
            recorder.record_step(tool="t", args={}, result={})

    def test_the_trajectory_id_is_derived_from_the_correlation_id(self):
        data = dataset()
        first = started_recorder(data.exceptions[0]).finish(outcome="resolved")
        second = started_recorder(data.exceptions[0]).finish(outcome="resolved")
        assert first.trajectory_id == second.trajectory_id

    def test_different_correlation_ids_give_different_trajectory_ids(self):
        data = dataset()
        first = started_recorder(data.exceptions[0]).finish(outcome="resolved")
        second = started_recorder(data.exceptions[1]).finish(outcome="resolved")
        assert first.trajectory_id != second.trajectory_id

    def test_a_recorded_error_step_keeps_its_typed_kind(self):
        data = dataset()
        recorder = started_recorder(data.exceptions[0])
        recorder.record_step(
            tool="get_settlement_record",
            args={"record_id": "REC-999999"},
            result=None,
            error=("RecordNotFoundError", "no settlement record"),
        )
        trajectory = recorder.finish(outcome="escalated")
        assert trajectory.steps[0].error is not None
        assert trajectory.steps[0].error.kind == "RecordNotFoundError"
        assert trajectory.steps[0].gate_verdict is GateVerdict.UNGATED


class TestStore:
    def test_the_in_memory_store_satisfies_the_protocol(self):
        store: TrajectoryStore = InMemoryTrajectoryStore()
        assert store.count() == 0

    def test_appended_trajectories_round_trip_unchanged(self):
        data = dataset()
        store = InMemoryTrajectoryStore()
        trajectory = started_recorder(data.exceptions[0]).finish(outcome="resolved")
        store.append(trajectory)
        assert store.all()[0] == trajectory

    def test_the_store_preserves_insertion_order(self):
        data = dataset()
        store = InMemoryTrajectoryStore()
        for exception in data.exceptions[:4]:
            store.append(started_recorder(exception).finish(outcome="resolved"))
        assert [t.correlation_id for t in store.all()] == [
            f"{e.exception_id}:run-0" for e in data.exceptions[:4]
        ]

    def test_appending_the_same_trajectory_twice_is_rejected(self):
        data = dataset()
        store = InMemoryTrajectoryStore()
        trajectory = started_recorder(data.exceptions[0]).finish(outcome="resolved")
        store.append(trajectory)
        with pytest.raises(RecorderError):
            store.append(trajectory)

    def test_the_store_returns_an_immutable_view(self):
        store = InMemoryTrajectoryStore()
        assert isinstance(store.all(), tuple)


class TestLabelling:
    def test_a_correctly_resolved_run_is_labelled_pass(self):
        data = dataset()
        tools = ReconciliationTools.from_snapshot(data.world)
        exception = data.exceptions[0]
        truth = data.ground_truths[0]
        resolve(tools, exception, truth)
        labelled = label_trajectory(
            started_recorder(exception).finish(outcome="resolved"),
            facts=exception.facts,
            ground_truth=truth,
            world=tools.snapshot(),
        )
        assert labelled.checker_verdict is CheckerVerdict.PASS
        assert labelled.checker_version == CHECKER_VERSION

    def test_an_untouched_world_is_labelled_undetermined(self):
        data = dataset()
        tools = ReconciliationTools.from_snapshot(data.world)
        exception = data.exceptions[0]
        labelled = label_trajectory(
            started_recorder(exception).finish(outcome="escalated"),
            facts=exception.facts,
            ground_truth=data.ground_truths[0],
            world=tools.snapshot(),
        )
        assert labelled.checker_verdict is CheckerVerdict.UNDETERMINED

    def test_labelling_changes_nothing_except_the_verdict(self):
        data = dataset()
        tools = ReconciliationTools.from_snapshot(data.world)
        exception = data.exceptions[0]
        before = started_recorder(exception).finish(outcome="resolved")
        after = label_trajectory(
            before,
            facts=exception.facts,
            ground_truth=data.ground_truths[0],
            world=tools.snapshot(),
        )
        assert after.model_dump(exclude={"checker_verdict", "checker_version"}) == (
            before.model_dump(exclude={"checker_verdict", "checker_version"})
        )

    def test_labelling_twice_is_rejected(self):
        data = dataset()
        tools = ReconciliationTools.from_snapshot(data.world)
        exception = data.exceptions[0]
        once = label_trajectory(
            started_recorder(exception).finish(outcome="resolved"),
            facts=exception.facts,
            ground_truth=data.ground_truths[0],
            world=tools.snapshot(),
        )
        with pytest.raises(RecorderError):
            label_trajectory(
                once,
                facts=exception.facts,
                ground_truth=data.ground_truths[0],
                world=tools.snapshot(),
            )
