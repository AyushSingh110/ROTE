from __future__ import annotations

from rote.contracts.evaluation import EvalPath, RunRecord
from rote.eval.harness import EvaluationOutput
from rote.eval.report import (
    accuracy,
    consistency,
    cost,
    deterministic_resolution,
    escalations,
    replay_fidelity,
)


def render(output: EvaluationOutput) -> str:
    rote = [record for record in output.runs if record.path is EvalPath.ROTE]
    live = [record for record in output.runs if record.path is EvalPath.LIVE_AGENT]
    lines: list[str] = []
    lines += _header(output)
    lines += _resolution(rote, live)
    lines += _consistency(output)
    lines += _escalations(rote, live)
    lines += _accuracy(output)
    lines += _replay(output)
    lines += _cost(output)
    lines += _routing(output)
    return "\n".join(lines)


def _header(output: EvaluationOutput) -> list[str]:
    return [
        "ROTE — FULL EVALUATION (ARCHITECTURE.md SS I)",
        "agent: offline-heuristic-1   classifier: structured-fields-double-1",
        "research grade: False — both models are stand-ins, so every number below is a",
        "mechanism measurement and none of it is evidence about real language models.",
        "",
        f"plans active: {output.active_plans}   plan bindings that need a slot: "
        f"{output.slot_bindings}",
        "",
    ]


def _resolution(rote: list[RunRecord], live: list[RunRecord]) -> list[str]:
    summary = deterministic_resolution(rote)
    baseline = deterministic_resolution(live)
    return [
        "I.1 DETERMINISTIC RESOLUTION RATE",
        f"  exceptions                              {summary.total}",
        f"  resolved by compiled code, 0 model calls {summary.resolved_deterministically}"
        f"  ({summary.rate_per_mille / 10:.1f}%)",
        f"  resolved by the live agent instead       {summary.resolved_by_the_live_agent}",
        f"  escalated                                {summary.escalated}",
        f"  failed                                   {summary.failed}",
        "",
        "  the claim is one bounded classification call, not zero model calls:",
        f"    Rote        classification {summary.classification_calls:>6}   "
        f"post-classification {summary.post_classification_calls:>6}",
        f"    live agent  classification {baseline.classification_calls:>6}   "
        f"post-classification {baseline.post_classification_calls:>6}",
        "",
    ]


def _consistency(output: EvaluationOutput) -> list[str]:
    lines = [
        "I.2 CONSISTENCY ACROSS REPEATED IDENTICAL RUNS",
        f"  {'cohort':<28}{'cases':>7}{'repeats':>9}{'one outcome':>13}"
        f"{'most distinct':>15}{'rate':>8}",
    ]
    for cohort in consistency(list(output.repeats)):
        lines.append(
            f"  {cohort.label:<28}{cohort.exceptions:>7}{cohort.repeats_each:>9}"
            f"{cohort.single_outcome:>13}{cohort.max_distinct:>15}"
            f"{cohort.rate_per_mille / 10:>7.1f}%"
        )
    for cohort in consistency(list(output.exploring_repeats)):
        lines.append(
            f"  {cohort.label + ', exploring':<28}{cohort.exceptions:>7}{cohort.repeats_each:>9}"
            f"{cohort.single_outcome:>13}{cohort.max_distinct:>15}"
            f"{cohort.rate_per_mille / 10:>7.1f}%"
        )
    lines.append("")
    return lines


def _escalations(rote: list[RunRecord], live: list[RunRecord]) -> list[str]:
    lines = ["I.3 ESCALATIONS, EVERY ONE WITH A NAMED REASON", "  Rote:"]
    counted = escalations(rote)
    for count in counted or ():
        lines.append(f"    {count.reason:<28} {count.count}")
    if not counted:
        lines.append("    nothing escalated")
    lines.append("  live agent:")
    baseline = escalations(live)
    for count in baseline or ():
        lines.append(f"    {count.reason:<28} {count.count}")
    if not baseline:
        lines.append("    nothing escalated")
    lines.append("")
    return lines


def _accuracy(output: EvaluationOutput) -> list[str]:
    report = accuracy(list(output.runs))
    return [
        "I.6 ACCURACY AGAINST THE CODE-ONLY CHECKER",
        f"  tasks compared on both paths   {report.tasks}",
        f"  Rote passed                    {report.rote_passed}",
        f"  live agent passed              {report.agent_passed}",
        f"  undetermined verdicts          {report.undetermined}   "
        "(escalation is safe, never a wrong answer)",
        "",
        "                     agent PASS   agent FAIL",
        f"    Rote PASS        {report.both_pass:>10}   {report.only_rote:>10}",
        f"    Rote FAIL        {report.only_agent:>10}   {report.both_fail:>10}",
        "",
    ]


def _replay(output: EvaluationOutput) -> list[str]:
    summary = replay_fidelity(list(output.replays))
    return [
        "I.5 AUDIT REPLAY FIDELITY",
        f"  plan-lifecycle ledger  {output.ledger_entries:>6} entries   "
        f"chain valid: {output.ledger_valid}",
        f"  money-movement ledger  {output.run_ledger_entries:>6} entries   "
        f"chain valid: {output.run_ledger_valid}",
        f"  compiled resolutions replayed  {summary.replayed}",
        f"  reproduced the same outcome    {summary.reproduced} "
        f"({summary.rate_per_mille / 10:.1f}%)",
        f"  reproduced the same gate keys  {summary.keys_reproduced}",
        "",
    ]


def _cost(output: EvaluationOutput) -> list[str]:
    lines = [
        "I.7 COST AND LATENCY (last, deliberately)",
        f"  {'path':<14}{'runs':>7}{'median calls':>14}{'p95 calls':>11}"
        f"{'median ms':>11}{'p95 ms':>9}",
    ]
    for row in cost(list(output.runs)):
        lines.append(
            f"  {row.path.value:<14}{row.runs:>7}{row.median_llm_calls:>14}"
            f"{row.p95_llm_calls:>11}{row.median_wall_ms:>11}{row.p95_wall_ms:>9}"
        )
    lines += [
        "  tokens are 0 on both paths: the stand-in reports none, so token cost is not",
        "  measurable here and the model-call count is the honest cost number.",
        "  wall-clock is not a fair comparison either: the tools are in-memory.",
        "",
    ]
    return lines


def _routing(output: EvaluationOutput) -> list[str]:
    lines = ["ROUTING — why each exception went where it did"]
    for reason, count in output.routes:
        lines.append(f"    {reason:<28} {count}")
    lines += [
        "",
        "CLASSIFIER CONFUSION (true -> predicted), the stand-in reading structured fields only",
    ]
    for true, predicted, count in output.confusion:
        marker = "  " if true == predicted else " *"
        lines.append(f"  {marker}{true:<24} -> {predicted:<24} {count}")
    lines.append("")
    return lines


__all__ = ["render"]
