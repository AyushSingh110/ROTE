from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    insert,
)
from sqlalchemy import (
    select as sql_select,
)
from sqlalchemy.exc import IntegrityError

from rote.contracts.canonical import canonical_bytes
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.errors import RecorderError
from rote.contracts.trajectory import Outcome, Trajectory
from rote.recorder.filters import matches

DATABASE_URL_ENV_VAR = "ROTE_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite+pysqlite:///rote-trajectories.db"

_metadata = MetaData()

# payload is the source of truth; the other columns are a projection kept only for selection
trajectories_table = Table(
    "trajectories",
    _metadata,
    Column("row_id", Integer, primary_key=True, autoincrement=True),
    Column("trajectory_id", Text, nullable=False, unique=True),
    Column("correlation_id", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("domain", Text, nullable=False),
    Column("executor_kind", Text, nullable=False),
    Column("agent_model_id", Text, nullable=False),
    Column("prompt_template_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("checker_verdict", Text, nullable=True),
    Column("dry_run", Boolean, nullable=False),
    Column("step_count", Integer, nullable=False),
    Column("payload", Text, nullable=False),
)


class SqlTrajectoryStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self._engine = create_engine(url)
        _metadata.create_all(self._engine)

    @classmethod
    def from_env(cls) -> SqlTrajectoryStore:
        return cls(os.environ.get(DATABASE_URL_ENV_VAR, DEFAULT_DATABASE_URL))

    def close(self) -> None:
        self._engine.dispose()

    def append(self, trajectory: Trajectory) -> None:
        payload = canonical_bytes(trajectory.model_dump(mode="json")).decode()
        statement = insert(trajectories_table).values(
            trajectory_id=str(trajectory.trajectory_id),
            correlation_id=trajectory.correlation_id,
            schema_version=trajectory.schema_version,
            domain=trajectory.domain.value,
            executor_kind=trajectory.executor_kind,
            agent_model_id=trajectory.agent_model_id,
            prompt_template_id=trajectory.prompt_template_id,
            outcome=trajectory.outcome,
            checker_verdict=(
                None if trajectory.checker_verdict is None else trajectory.checker_verdict.value
            ),
            dry_run=trajectory.dry_run,
            step_count=len(trajectory.steps),
            payload=payload,
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(statement)
        except IntegrityError as error:
            raise RecorderError(
                f"trajectory {trajectory.trajectory_id} is already stored"
            ) from error

    def all(self) -> tuple[Trajectory, ...]:
        return tuple(self._load_all())

    def count(self) -> int:
        with self._engine.connect() as connection:
            rows = connection.execute(sql_select(trajectories_table.c.row_id)).all()
        return len(rows)

    def select(
        self,
        *,
        domain: Domain | None = None,
        outcome: Outcome | None = None,
        verdict: CheckerVerdict | None = None,
        agent_model_id: str | None = None,
    ) -> tuple[Trajectory, ...]:
        return tuple(
            trajectory
            for trajectory in self._load_all()
            if matches(
                trajectory,
                domain=domain,
                outcome=outcome,
                verdict=verdict,
                agent_model_id=agent_model_id,
            )
        )

    def get(self, trajectory_id: str) -> Trajectory:
        statement = sql_select(trajectories_table.c.payload).where(
            trajectories_table.c.trajectory_id == trajectory_id
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).first()
        if row is None:
            raise RecorderError(f"no trajectory {trajectory_id!r}")
        return _rebuild(row[0])

    def projection(self) -> tuple[dict[str, Any], ...]:
        statement = sql_select(trajectories_table).order_by(trajectories_table.c.row_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(dict(row) for row in rows)

    def _load_all(self) -> list[Trajectory]:
        statement = sql_select(trajectories_table.c.payload).order_by(trajectories_table.c.row_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [_rebuild(row[0]) for row in rows]


def _rebuild(payload: str) -> Trajectory:
    return Trajectory.model_validate(json.loads(payload))
