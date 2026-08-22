from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain
from rote.contracts.trajectory import (
    GateVerdict,
    ToolErrorRecord,
    Trajectory,
    TrajectoryStep,
)

MOMENT = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_step(**overrides: object) -> TrajectoryStep:
    fields: dict[str, object] = {
        "index": 0,
        "tool": "get_settlement_record",
        "args": {"record_id": "REC-000000"},
        "result": {"record": {"record_id": "REC-000000"}},
        "result_fingerprint": "a" * 64,
        "gate_verdict": GateVerdict.UNGATED,
        "idempotency_key": None,
        "error": None,
        "attempts": 1,
        "latency_ms": 3,
    }
    fields.update(overrides)
    return TrajectoryStep(**fields)


def make_trajectory(**overrides: object) -> Trajectory:
    fields: dict[str, object] = {
        "trajectory_id": UUID("00000000-0000-5000-8000-000000000001"),
        "correlation_id": "EXC-000000:run-0",
        "domain": Domain.RECONCILIATION,
        "executor_kind": "live_agent",
        "task_input_redacted": {"record_id": "REC-000000"},
        "untrusted_text_paths": ("$.merchant_note",),
        "category": None,
        "category_confidence": None,
        "steps": (make_step(),),
        "outcome": "resolved",
        "checker_verdict": None,
        "checker_version": None,
        "agent_model_id": "offline-heuristic-1",
        "prompt_template_id": "offline-v1",
        "dry_run": True,
        "started_at": MOMENT,
        "finished_at": MOMENT,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    fields.update(overrides)
    return Trajectory(**fields)


class TestTrajectoryStep:
    def test_a_well_formed_step_is_accepted(self):
        assert make_step().tool == "get_settlement_record"

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            make_step(surprise=1)

    def test_the_step_is_frozen(self):
        with pytest.raises(ValidationError):
            make_step().index = 5

    def test_a_negative_index_is_rejected(self):
        with pytest.raises(ValidationError):
            make_step(index=-1)

    def test_a_short_fingerprint_is_rejected(self):
        with pytest.raises(ValidationError):
            make_step(result_fingerprint="abc")

    def test_attempts_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            make_step(attempts=0)

    def test_an_error_record_can_be_attached(self):
        step = make_step(error=ToolErrorRecord(kind="RecordNotFoundError", message="missing"))
        assert step.error is not None
        assert step.error.kind == "RecordNotFoundError"

    def test_every_step_states_whether_it_was_gated(self):
        assert set(GateVerdict) >= {
            GateVerdict.UNGATED,
            GateVerdict.PERMIT,
            GateVerdict.REFUSE,
            GateVerdict.ESCALATE,
        }
        assert "gate_verdict" in TrajectoryStep.model_fields


class TestTrajectory:
    def test_a_well_formed_trajectory_is_accepted(self):
        assert make_trajectory().schema_version == 1

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            make_trajectory(surprise=1)

    def test_the_trajectory_is_frozen(self):
        with pytest.raises(ValidationError):
            make_trajectory().outcome = "failed"

    def test_an_unknown_outcome_is_rejected(self):
        with pytest.raises(ValidationError):
            make_trajectory(outcome="mostly_fine")

    def test_a_naive_timestamp_is_rejected(self):
        with pytest.raises(ValidationError):
            make_trajectory(started_at=datetime(2026, 8, 22, 10, 0, 0))

    def test_finishing_before_starting_is_rejected(self):
        with pytest.raises(ValidationError):
            make_trajectory(finished_at=datetime(2026, 8, 22, 9, 0, 0, tzinfo=UTC))

    def test_step_indices_must_be_dense_and_ordered(self):
        with pytest.raises(ValidationError):
            make_trajectory(steps=(make_step(index=0), make_step(index=2)))

    def test_the_never_backfillable_fields_are_all_present(self):
        for field in (
            "schema_version",
            "agent_model_id",
            "prompt_template_id",
            "untrusted_text_paths",
            "checker_verdict",
            "checker_version",
            "dry_run",
        ):
            assert field in Trajectory.model_fields

    def test_an_unlabelled_trajectory_carries_no_verdict(self):
        trajectory = make_trajectory()
        assert trajectory.checker_verdict is None
        assert trajectory.checker_version is None

    def test_a_labelled_trajectory_carries_both_verdict_and_version(self):
        with pytest.raises(ValidationError):
            make_trajectory(checker_verdict=CheckerVerdict.PASS, checker_version=None)
