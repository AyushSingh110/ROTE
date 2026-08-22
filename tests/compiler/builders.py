from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.fingerprint import structural_fingerprint
from rote.contracts.trajectory import (
    GateVerdict,
    Outcome,
    ToolErrorRecord,
    Trajectory,
    TrajectoryStep,
)

MOMENT = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
NAMESPACE = uuid5(NAMESPACE_URL, "https://rote.invalid/test-trajectory")

# invented tool names on purpose: the probe must never assume the real reconciliation tool set
ALPHA = ("alpha", "beta", "gamma")
DELTA = ("alpha", "delta")


def build(
    name: str,
    tools: Sequence[str],
    *,
    outcome: Outcome = "resolved",
    verdict: CheckerVerdict | None = CheckerVerdict.PASS,
    domain: Domain = Domain.RECONCILIATION,
    model: str = "some-real-model",
    failed_at: int | None = None,
) -> Trajectory:
    steps = tuple(
        TrajectoryStep(
            index=index,
            tool=tool,
            args={"n": index},
            result=None if failed_at == index else {"ok": True},
            result_fingerprint=f"{index:064d}",
            gate_verdict=GateVerdict.PERMIT,
            idempotency_key=None,
            error=(
                ToolErrorRecord(kind="ToolError", message="boom") if failed_at == index else None
            ),
            attempts=1,
            latency_ms=1,
        )
        for index, tool in enumerate(tools)
    )
    return Trajectory(
        trajectory_id=uuid5(NAMESPACE, name),
        correlation_id=name,
        domain=domain,
        executor_kind="live_agent",
        task_input_redacted={"case": name},
        untrusted_text_paths=("$.merchant_note",),
        category=None,
        category_confidence=None,
        steps=steps,
        outcome=outcome,
        checker_verdict=verdict,
        checker_version=None if verdict is None else "reconciliation-1",
        agent_model_id=model,
        prompt_template_id="test-v1",
        dry_run=True,
        started_at=MOMENT,
        finished_at=MOMENT,
        tokens_in=0,
        tokens_out=0,
    )


def population(
    modal: Sequence[str],
    modal_count: int,
    others: Sequence[tuple[Sequence[str], int]] = (),
    *,
    model: str = "some-real-model",
) -> list[Trajectory]:
    built = [build(f"modal-{i}", modal, model=model) for i in range(modal_count)]
    for group, (tools, count) in enumerate(others):
        built.extend(build(f"other-{group}-{i}", tools, model=model) for i in range(count))
    return built


def categories_all(
    trajectories: Sequence[Trajectory],
    category: ExceptionCategory = ExceptionCategory.FEE_MISMATCH,
) -> dict[UUID, ExceptionCategory]:
    return {trajectory.trajectory_id: category for trajectory in trajectories}


def build_with_steps(
    name: str,
    steps: Sequence[tuple[str, dict[str, object], dict[str, object]]],
    *,
    task_input: dict[str, object],
    model: str = "some-real-model",
) -> Trajectory:
    recorded = tuple(
        TrajectoryStep(
            index=index,
            tool=tool,
            args=dict(args),
            result=dict(result),
            result_fingerprint=structural_fingerprint(dict(result)),
            gate_verdict=GateVerdict.PERMIT,
            idempotency_key=None,
            error=None,
            attempts=1,
            latency_ms=1,
        )
        for index, (tool, args, result) in enumerate(steps)
    )
    return Trajectory(
        trajectory_id=uuid5(NAMESPACE, name),
        correlation_id=name,
        domain=Domain.RECONCILIATION,
        executor_kind="live_agent",
        task_input_redacted=dict(task_input),
        untrusted_text_paths=("$.merchant_note",),
        category=None,
        category_confidence=None,
        steps=recorded,
        outcome="resolved",
        checker_verdict=CheckerVerdict.PASS,
        checker_version="reconciliation-1",
        agent_model_id=model,
        prompt_template_id="test-v1",
        dry_run=True,
        started_at=MOMENT,
        finished_at=MOMENT,
        tokens_in=0,
        tokens_out=0,
    )
