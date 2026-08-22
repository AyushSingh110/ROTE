from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import RecorderError
from rote.contracts.fingerprint import structural_fingerprint
from rote.contracts.trajectory import (
    GateVerdict,
    Outcome,
    ToolErrorRecord,
    Trajectory,
    TrajectoryStep,
)

TRAJECTORY_NAMESPACE = uuid5(NAMESPACE_URL, "https://rote.invalid/trajectory")


def _system_clock() -> datetime:
    return datetime.now(UTC)


class TrajectoryRecorder:
    def __init__(self, clock: Callable[[], datetime] = _system_clock) -> None:
        self._clock = clock
        self._started_at: datetime | None = None
        self._finished = False
        self._steps: list[TrajectoryStep] = []
        self._header: dict[str, Any] = {}
        self._tokens_in = 0
        self._tokens_out = 0

    def start(
        self,
        *,
        correlation_id: str,
        domain: Domain,
        task_input_redacted: Mapping[str, Any],
        untrusted_text_paths: tuple[str, ...],
        agent_model_id: str,
        prompt_template_id: str,
        dry_run: bool,
        executor_kind: str = "live_agent",
    ) -> None:
        if self._started_at is not None:
            raise RecorderError("this recorder has already started a trajectory")
        self._started_at = self._clock()
        self._header = {
            "correlation_id": correlation_id,
            "domain": domain,
            "task_input_redacted": dict(task_input_redacted),
            "untrusted_text_paths": untrusted_text_paths,
            "agent_model_id": agent_model_id,
            "prompt_template_id": prompt_template_id,
            "dry_run": dry_run,
            "executor_kind": executor_kind,
        }

    # no fingerprint parameter on purpose: one code path computes them, so the compiler and
    # the guard can never disagree about what a result looks like
    def record_step(
        self,
        *,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any] | None,
        error: tuple[str, str] | None = None,
        gate_verdict: GateVerdict = GateVerdict.UNGATED,
        idempotency_key: str | None = None,
        attempts: int = 1,
        latency_ms: int = 0,
    ) -> None:
        self._require_open()
        payload = dict(result) if result is not None else None
        self._steps.append(
            TrajectoryStep(
                index=len(self._steps),
                tool=tool,
                args=dict(args),
                result=payload,
                result_fingerprint=structural_fingerprint(payload),
                gate_verdict=gate_verdict,
                idempotency_key=idempotency_key,
                error=None if error is None else ToolErrorRecord(kind=error[0], message=error[1]),
                attempts=attempts,
                latency_ms=latency_ms,
            )
        )

    def record_usage(self, tokens_in: int, tokens_out: int) -> None:
        self._require_open()
        self._tokens_in += tokens_in
        self._tokens_out += tokens_out

    def finish(
        self,
        *,
        outcome: Outcome,
        category: ExceptionCategory | None = None,
        category_confidence: float | None = None,
    ) -> Trajectory:
        self._require_open()
        self._finished = True
        started_at = self._started_at
        assert started_at is not None
        return Trajectory(
            trajectory_id=uuid5(TRAJECTORY_NAMESPACE, self._header["correlation_id"]),
            category=category,
            category_confidence=category_confidence,
            steps=tuple(self._steps),
            outcome=outcome,
            checker_verdict=None,
            checker_version=None,
            started_at=started_at,
            finished_at=self._clock(),
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            **self._header,
        )

    def _require_open(self) -> None:
        if self._started_at is None:
            raise RecorderError("the recorder was not started")
        if self._finished:
            raise RecorderError("this trajectory is already finished")


__all__ = ["UUID", "TrajectoryRecorder"]
