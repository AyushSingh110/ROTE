import inspect
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rote.contracts.canonical import canonical_bytes
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import RecorderError
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.trajectory import Trajectory, TrajectoryStore
from rote.domain.generators.reconciliation import generate_dataset
from rote.recorder.recorder import TrajectoryRecorder
from rote.recorder.sql_store import DEFAULT_DATABASE_URL, SqlTrajectoryStore
from rote.recorder.store import InMemoryTrajectoryStore

SEED = 23
COUNT = 120

StoreFactory = Callable[[], TrajectoryStore]


def ticking_clock() -> Iterator[datetime]:
    moment = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(milliseconds=4)


def dataset(count: int = COUNT) -> GeneratedDataset:
    return generate_dataset(seed=SEED, count=count)


def make_trajectory(
    exception: ReconciliationException,
    *,
    run_tag: str = "run-0",
    outcome: str = "resolved",
    agent_model_id: str = "offline-heuristic-1",
    verdict: CheckerVerdict | None = None,
) -> Trajectory:
    source = ticking_clock()
    recorder = TrajectoryRecorder(clock=lambda: next(source))
    recorder.start(
        correlation_id=f"{exception.exception_id}:{run_tag}",
        domain=Domain.RECONCILIATION,
        task_input_redacted=exception.facts.model_dump(mode="json"),
        untrusted_text_paths=tuple(block.source_path for block in exception.untrusted),
        agent_model_id=agent_model_id,
        prompt_template_id="offline-v1",
        dry_run=True,
    )
    recorder.record_step(
        tool="get_settlement_record",
        args={"record_id": exception.facts.record_id},
        result={"record": {"record_id": exception.facts.record_id}},
    )
    recorder.record_usage(13, 7)
    trajectory = recorder.finish(outcome=outcome)  # type: ignore[arg-type]
    if verdict is None:
        return trajectory
    return trajectory.model_copy(
        update={"checker_verdict": verdict, "checker_version": "reconciliation-1"}
    )


@pytest.fixture
def sql_store(tmp_path: Path) -> Iterator[SqlTrajectoryStore]:
    store = SqlTrajectoryStore(f"sqlite+pysqlite:///{tmp_path / 'trajectories.db'}")
    yield store
    store.close()


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[TrajectoryStore]:
    if request.param == "memory":
        yield InMemoryTrajectoryStore()
        return
    built = SqlTrajectoryStore(f"sqlite+pysqlite:///{tmp_path / 'shared.db'}")
    yield built
    built.close()


class TestBothStoresHonourTheSameContract:
    def test_a_new_store_is_empty(self, store: TrajectoryStore) -> None:
        assert store.count() == 0
        assert store.all() == ()

    def test_an_appended_trajectory_comes_back_equal(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        trajectory = make_trajectory(data.exceptions[0])
        store.append(trajectory)
        assert store.all() == (trajectory,)

    def test_insertion_order_is_preserved(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        for exception in data.exceptions:
            store.append(make_trajectory(exception))
        assert [t.correlation_id for t in store.all()] == [
            f"{e.exception_id}:run-0" for e in data.exceptions
        ]

    def test_appending_the_same_trajectory_twice_is_rejected(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        trajectory = make_trajectory(data.exceptions[0])
        store.append(trajectory)
        with pytest.raises(RecorderError):
            store.append(trajectory)

    def test_all_returns_an_immutable_tuple(self, store: TrajectoryStore) -> None:
        assert isinstance(store.all(), tuple)

    def test_the_store_exposes_no_update_or_delete(self, store: TrajectoryStore) -> None:
        for forbidden in ("update", "delete", "remove", "pop", "clear", "truncate", "drop"):
            assert not hasattr(store, forbidden)

    def test_selection_without_filters_returns_everything(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        for exception in data.exceptions:
            store.append(make_trajectory(exception))
        assert store.select() == store.all()

    def test_selection_by_verdict_returns_only_that_verdict(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        store.append(make_trajectory(data.exceptions[0], verdict=CheckerVerdict.PASS))
        store.append(make_trajectory(data.exceptions[1], verdict=CheckerVerdict.FAIL))
        store.append(make_trajectory(data.exceptions[2], verdict=CheckerVerdict.UNDETERMINED))
        chosen = store.select(verdict=CheckerVerdict.PASS)
        assert len(chosen) == 1
        assert chosen[0].checker_verdict is CheckerVerdict.PASS

    def test_unlabelled_trajectories_are_never_selected_by_verdict(
        self, store: TrajectoryStore
    ) -> None:
        data = dataset(6)
        store.append(make_trajectory(data.exceptions[0]))
        assert store.select(verdict=CheckerVerdict.PASS) == ()

    def test_selection_by_model_separates_producing_models(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        store.append(make_trajectory(data.exceptions[0], agent_model_id="offline-heuristic-1"))
        store.append(make_trajectory(data.exceptions[1], agent_model_id="some-real-model"))
        chosen = store.select(agent_model_id="some-real-model")
        assert len(chosen) == 1
        assert chosen[0].agent_model_id == "some-real-model"

    def test_selection_by_outcome_works(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        store.append(make_trajectory(data.exceptions[0], outcome="resolved"))
        store.append(make_trajectory(data.exceptions[1], outcome="escalated"))
        assert len(store.select(outcome="escalated")) == 1

    def test_filters_combine_as_and(self, store: TrajectoryStore) -> None:
        data = dataset(6)
        store.append(
            make_trajectory(data.exceptions[0], outcome="resolved", verdict=CheckerVerdict.PASS)
        )
        store.append(
            make_trajectory(data.exceptions[1], outcome="escalated", verdict=CheckerVerdict.PASS)
        )
        assert len(store.select(outcome="resolved", verdict=CheckerVerdict.PASS)) == 1

    def test_selection_preserves_insertion_order(self, store: TrajectoryStore) -> None:
        data = dataset(8)
        for exception in data.exceptions:
            store.append(make_trajectory(exception, verdict=CheckerVerdict.PASS))
        chosen = store.select(verdict=CheckerVerdict.PASS)
        assert [t.correlation_id for t in chosen] == [
            f"{e.exception_id}:run-0" for e in data.exceptions
        ]


class TestRoundTripFidelity:
    def test_a_hundred_trajectories_round_trip_byte_identical(
        self, sql_store: SqlTrajectoryStore
    ) -> None:
        data = dataset(COUNT)
        written = [
            make_trajectory(exception, verdict=CheckerVerdict.PASS) for exception in data.exceptions
        ]
        for trajectory in written:
            sql_store.append(trajectory)
        loaded = sql_store.all()
        assert len(loaded) == COUNT
        for before, after in zip(written, loaded, strict=True):
            assert canonical_bytes(before.model_dump(mode="json")) == canonical_bytes(
                after.model_dump(mode="json")
            )

    def test_round_tripped_trajectories_compare_equal(self, sql_store: SqlTrajectoryStore) -> None:
        data = dataset(20)
        written = [make_trajectory(e, verdict=CheckerVerdict.PASS) for e in data.exceptions]
        for trajectory in written:
            sql_store.append(trajectory)
        assert list(sql_store.all()) == written

    def test_the_never_backfillable_fields_survive_the_round_trip(
        self, sql_store: SqlTrajectoryStore
    ) -> None:
        data = dataset(6)
        original = make_trajectory(data.exceptions[0], verdict=CheckerVerdict.PASS)
        sql_store.append(original)
        loaded = sql_store.all()[0]
        assert loaded.schema_version == original.schema_version
        assert loaded.agent_model_id == original.agent_model_id
        assert loaded.prompt_template_id == original.prompt_template_id
        assert loaded.untrusted_text_paths == original.untrusted_text_paths
        assert loaded.dry_run == original.dry_run
        assert loaded.checker_verdict == original.checker_verdict
        assert loaded.checker_version == original.checker_version

    def test_timestamps_and_ids_survive_exactly(self, sql_store: SqlTrajectoryStore) -> None:
        data = dataset(6)
        original = make_trajectory(data.exceptions[0])
        sql_store.append(original)
        loaded = sql_store.all()[0]
        assert loaded.trajectory_id == original.trajectory_id
        assert loaded.started_at == original.started_at
        assert loaded.finished_at == original.finished_at

    def test_step_detail_survives_exactly(self, sql_store: SqlTrajectoryStore) -> None:
        data = dataset(6)
        original = make_trajectory(data.exceptions[0])
        sql_store.append(original)
        loaded = sql_store.all()[0]
        assert loaded.steps == original.steps


class TestDurability:
    def test_a_second_store_over_the_same_file_sees_the_data(self, tmp_path: Path) -> None:
        url = f"sqlite+pysqlite:///{tmp_path / 'durable.db'}"
        data = dataset(10)
        writer = SqlTrajectoryStore(url)
        for exception in data.exceptions:
            writer.append(make_trajectory(exception))
        writer.close()

        reader = SqlTrajectoryStore(url)
        try:
            assert reader.count() == 10
            assert reader.all()[0].correlation_id == f"{data.exceptions[0].exception_id}:run-0"
        finally:
            reader.close()

    def test_opening_an_existing_store_does_not_wipe_it(self, tmp_path: Path) -> None:
        url = f"sqlite+pysqlite:///{tmp_path / 'twice.db'}"
        data = dataset(6)
        first = SqlTrajectoryStore(url)
        first.append(make_trajectory(data.exceptions[0]))
        first.close()
        second = SqlTrajectoryStore(url)
        try:
            second.append(make_trajectory(data.exceptions[1]))
            assert second.count() == 2
        finally:
            second.close()

    def test_a_duplicate_is_rejected_across_process_boundaries(self, tmp_path: Path) -> None:
        url = f"sqlite+pysqlite:///{tmp_path / 'dupe.db'}"
        data = dataset(6)
        trajectory = make_trajectory(data.exceptions[0])
        first = SqlTrajectoryStore(url)
        first.append(trajectory)
        first.close()
        second = SqlTrajectoryStore(url)
        try:
            with pytest.raises(RecorderError):
                second.append(trajectory)
        finally:
            second.close()


class TestPayloadIsTheSourceOfTruth:
    def test_the_indexed_columns_agree_with_the_stored_payload(
        self, sql_store: SqlTrajectoryStore
    ) -> None:
        data = dataset(20)
        for exception in data.exceptions:
            sql_store.append(make_trajectory(exception, verdict=CheckerVerdict.PASS))
        for row in sql_store.projection():
            trajectory = sql_store.get(row["trajectory_id"])
            assert row["correlation_id"] == trajectory.correlation_id
            assert row["outcome"] == trajectory.outcome
            assert row["agent_model_id"] == trajectory.agent_model_id
            assert row["step_count"] == len(trajectory.steps)

    def test_fetching_an_unknown_trajectory_raises(self, sql_store: SqlTrajectoryStore) -> None:
        with pytest.raises(RecorderError):
            sql_store.get("00000000-0000-5000-8000-00000000dead")


class TestConfiguration:
    def test_the_database_url_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        url = f"sqlite+pysqlite:///{tmp_path / 'from-env.db'}"
        monkeypatch.setenv("ROTE_DATABASE_URL", url)
        store = SqlTrajectoryStore.from_env()
        try:
            assert store.url == url
        finally:
            store.close()

    def test_without_the_environment_variable_a_documented_default_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ROTE_DATABASE_URL", raising=False)
        assert DEFAULT_DATABASE_URL.startswith("sqlite")

    def test_no_credential_ever_appears_in_the_default(self) -> None:
        for secret_marker in ("password", "@", "token", "key="):
            assert secret_marker not in DEFAULT_DATABASE_URL

    def test_the_protocol_requires_selection(self) -> None:
        assert "select" in dir(TrajectoryStore)
        rendered = str(inspect.signature(SqlTrajectoryStore.select))
        assert "verdict" in rendered
        assert "agent_model_id" in rendered
