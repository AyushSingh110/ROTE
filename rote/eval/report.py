from __future__ import annotations

import collections
from collections.abc import Sequence

from rote.contracts.checker import CheckerVerdict
from rote.contracts.evaluation import (
    FULL_SCALE,
    AccuracyReport,
    ConsistencyCohort,
    CostSummary,
    EscalationCount,
    EvalPath,
    RepeatRecord,
    ReplayRecord,
    ReplaySummary,
    ResolutionSummary,
    RunRecord,
    TerminalState,
)

SLOT_FREE = "compiled, slot-free"
SLOT_BEARING = "compiled, contains a slot"
LIVE = "live agent"
COHORT_ORDER = (SLOT_FREE, SLOT_BEARING, LIVE)


# I.1 — the headline. A compiled resolution that needed a model call is not deterministic,
# whatever the terminal state says, so the call count is part of the test and not a footnote.
def deterministic_resolution(records: Sequence[RunRecord]) -> ResolutionSummary:
    terminals = collections.Counter(record.terminal_state for record in records)
    deterministic = sum(
        1
        for record in records
        if record.terminal_state is TerminalState.RESOLVED_COMPILED
        and record.llm_calls_post_classification == 0
    )
    return ResolutionSummary(
        total=len(records),
        resolved_deterministically=deterministic,
        resolved_by_the_live_agent=terminals[TerminalState.RESOLVED_LIVE],
        escalated=terminals[TerminalState.ESCALATED],
        failed=terminals[TerminalState.FAILED],
        classification_calls=sum(record.llm_calls_classification for record in records),
        post_classification_calls=sum(record.llm_calls_post_classification for record in records),
        rate_per_mille=_rate(deterministic, len(records)),
    )


# I.2 — three cohorts, reported apart. Blurring them would claim determinism for plans that
# do not have it, which is the one thing the pitch must not do.
def consistency(repeats: Sequence[RepeatRecord]) -> tuple[ConsistencyCohort, ...]:
    grouped: dict[str, dict[str, list[str]]] = {label: {} for label in COHORT_ORDER}
    for record in repeats:
        grouped[_cohort_of(record)].setdefault(record.task_id, []).append(record.outcome_hash)

    cohorts = []
    for label in COHORT_ORDER:
        tasks = grouped[label]
        if not tasks:
            continue
        distinct = {task: len(set(hashes)) for task, hashes in tasks.items()}
        single = sum(1 for count in distinct.values() if count == 1)
        cohorts.append(
            ConsistencyCohort(
                label=label,
                exceptions=len(tasks),
                repeats_each=min(len(hashes) for hashes in tasks.values()),
                single_outcome=single,
                max_distinct=max(distinct.values()),
                rate_per_mille=_rate(single, len(tasks)),
            )
        )
    return tuple(cohorts)


# I.3 — high escalation is honest, unexplained escalation is the failure
def escalations(records: Sequence[RunRecord]) -> tuple[EscalationCount, ...]:
    counts = collections.Counter(
        record.escalation_reason
        for record in records
        if record.terminal_state is TerminalState.ESCALATED and record.escalation_reason
    )
    return tuple(
        EscalationCount(reason=reason, count=count) for reason, count in counts.most_common()
    )


# I.6 — same task set, both paths. The 2x2 is the point: the only-agent cell is next version's work.
def accuracy(records: Sequence[RunRecord]) -> AccuracyReport:
    by_task: dict[str, dict[EvalPath, CheckerVerdict]] = {}
    for record in records:
        by_task.setdefault(record.task_id, {})[record.path] = record.checker_verdict

    cells = collections.Counter[str]()
    undetermined = 0
    for task, verdicts in sorted(by_task.items()):
        if set(verdicts) != set(EvalPath):
            raise ValueError(f"{task} was not run on both paths, so it cannot be compared")
        rote, agent = verdicts[EvalPath.ROTE], verdicts[EvalPath.LIVE_AGENT]
        undetermined += sum(
            1 for verdict in (rote, agent) if verdict is CheckerVerdict.UNDETERMINED
        )
        if CheckerVerdict.UNDETERMINED in (rote, agent):
            continue
        cells[f"{rote is CheckerVerdict.PASS}:{agent is CheckerVerdict.PASS}"] += 1

    return AccuracyReport(
        tasks=len(by_task),
        rote_passed=cells["True:True"] + cells["True:False"],
        agent_passed=cells["True:True"] + cells["False:True"],
        both_pass=cells["True:True"],
        both_fail=cells["False:False"],
        only_rote=cells["True:False"],
        only_agent=cells["False:True"],
        undetermined=undetermined,
    )


# I.5 — two numbers, both reported: the chain verifies separately, this is the replay half
def replay_fidelity(replays: Sequence[ReplayRecord]) -> ReplaySummary:
    reproduced = sum(1 for record in replays if record.reproduced)
    return ReplaySummary(
        replayed=len(replays),
        reproduced=reproduced,
        keys_reproduced=sum(1 for record in replays if record.idempotency_keys_match),
        rate_per_mille=_rate(reproduced, len(replays)),
    )


# I.7 — last, deliberately, and never blended across paths
def cost(records: Sequence[RunRecord]) -> tuple[CostSummary, ...]:
    grouped: dict[EvalPath, list[RunRecord]] = {}
    for record in records:
        grouped.setdefault(record.path, []).append(record)

    summaries = []
    for path in EvalPath:
        rows = grouped.get(path)
        if not rows:
            continue
        calls = [r.llm_calls_classification + r.llm_calls_post_classification for r in rows]
        summaries.append(
            CostSummary(
                path=path,
                runs=len(rows),
                median_llm_calls=_percentile(calls, 50),
                p95_llm_calls=_percentile(calls, 95),
                median_tokens=_percentile([r.tokens_in + r.tokens_out for r in rows], 50),
                median_wall_ms=_percentile([r.wall_ms_excluding_tool_io for r in rows], 50),
                p95_wall_ms=_percentile([r.wall_ms_excluding_tool_io for r in rows], 95),
            )
        )
    return tuple(summaries)


def _cohort_of(record: RepeatRecord) -> str:
    if record.path is EvalPath.LIVE_AGENT:
        return LIVE
    return SLOT_BEARING if record.slot_call_count > 0 else SLOT_FREE


# nearest-rank, so every reported value is a value that actually occurred
def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return ordered[min(rank, len(ordered)) - 1]


def _rate(count: int, total: int) -> int:
    return 0 if total == 0 else count * FULL_SCALE // total


__all__ = [
    "accuracy",
    "consistency",
    "cost",
    "deterministic_resolution",
    "escalations",
    "replay_fidelity",
]
