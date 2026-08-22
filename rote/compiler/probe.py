from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rote.compiler.selection import EligibilityReport, select_eligible
from rote.compiler.sequences import SequenceGroup, group_by_sequence, sequence_of
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.trajectory import Trajectory

FROZEN = ConfigDict(extra="forbid", frozen=True)
MAX_ALTERNATIVES = 5


class CompilabilityVerdict(StrEnum):
    COMPILABLE = "compilable"
    PREFIX_ONLY = "prefix_only"
    NON_COMPILABLE = "non_compilable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


# ratios are stored per mille, never as floats, so a report stays canonically comparable
class ProbeThresholds(BaseModel):
    model_config = FROZEN

    compilable_support_per_mille: int = Field(gt=0, le=1000)
    prefix_support_per_mille: int = Field(gt=0, le=1000)
    floor_support_per_mille: int = Field(gt=0, le=1000)
    min_eligible: int = Field(gt=0)


class CategoryProbe(BaseModel):
    model_config = FROZEN

    category: ExceptionCategory
    eligible: int
    collapsed: bool
    modal_sequence: tuple[str, ...]
    modal_count: int
    prefix: tuple[str, ...]
    prefix_count: int
    alternatives: tuple[SequenceGroup, ...]
    verdict: CompilabilityVerdict

    @property
    def modal_support(self) -> float:
        return 0.0 if self.eligible == 0 else self.modal_count / self.eligible

    @property
    def prefix_support(self) -> float:
        return 0.0 if self.eligible == 0 else self.prefix_count / self.eligible


class ProbeReport(BaseModel):
    model_config = FROZEN

    domain: Domain
    agent_model_id: str
    thresholds: ProbeThresholds
    eligibility: EligibilityReport
    categories: tuple[CategoryProbe, ...]


def run_probe(
    trajectories: Sequence[Trajectory],
    *,
    category_of: Mapping[UUID, ExceptionCategory],
    domain: Domain,
    thresholds: ProbeThresholds,
) -> ProbeReport:
    model_id = _single_model(trajectories)
    eligible, eligibility = select_eligible(trajectories, domain=domain)
    _require_categories(eligible, category_of)

    by_category: dict[ExceptionCategory, list[Trajectory]] = {}
    for trajectory in trajectories:
        category = category_of.get(trajectory.trajectory_id)
        if category is not None:
            by_category.setdefault(category, [])
    for trajectory in eligible:
        by_category[category_of[trajectory.trajectory_id]].append(trajectory)

    return ProbeReport(
        domain=domain,
        agent_model_id=model_id,
        thresholds=thresholds,
        eligibility=eligibility,
        categories=tuple(
            _probe_category(category, by_category[category], thresholds)
            for category in sorted(by_category)
        ),
    )


def research_grade(report: ProbeReport, *, test_double_model_ids: frozenset[str]) -> bool:
    return report.agent_model_id not in test_double_model_ids


def format_probe_report(report: ProbeReport) -> str:
    lines = [
        f"compilability probe  domain={report.domain.value}  model={report.agent_model_id}",
        f"eligible {report.eligibility.eligible} of {report.eligibility.examined} examined"
        f"  rejected {dict(report.eligibility.rejected)}",
        "",
        f"{'category':<22}{'eligible':>9}{'support':>9}{'prefix':>8}{'prefixsup':>11}"
        f"  {'verdict':<22}modal sequence",
    ]
    for result in report.categories:
        lines.append(
            f"{result.category.value:<22}{result.eligible:>9}{result.modal_support:>9.2f}"
            f"{len(result.prefix):>8}{result.prefix_support:>11.2f}"
            f"  {result.verdict.value:<22}{'>'.join(result.modal_sequence) or '-'}"
        )
    return "\n".join(lines)


def _probe_category(
    category: ExceptionCategory,
    trajectories: Sequence[Trajectory],
    thresholds: ProbeThresholds,
) -> CategoryProbe:
    if not trajectories:
        return _empty(category, thresholds)

    # try both readings of the same runs and keep whichever explains more of them
    best = max(
        (_measure(trajectories, collapse=option) for option in (False, True)),
        key=lambda measured: measured[0][0].count,
    )
    groups, collapsed = best
    modal = groups[0].sequence
    prefix, prefix_count = _best_prefix(trajectories, modal, collapsed, thresholds)

    return CategoryProbe(
        category=category,
        eligible=len(trajectories),
        collapsed=collapsed,
        modal_sequence=modal,
        modal_count=groups[0].count,
        prefix=prefix,
        prefix_count=prefix_count,
        alternatives=tuple(groups[1 : 1 + MAX_ALTERNATIVES]),
        verdict=_verdict(len(trajectories), groups[0].count, prefix, prefix_count, thresholds),
    )


def _measure(
    trajectories: Sequence[Trajectory], *, collapse: bool
) -> tuple[tuple[SequenceGroup, ...], bool]:
    return group_by_sequence(trajectories, collapse=collapse), collapse


def _best_prefix(
    trajectories: Sequence[Trajectory],
    modal: tuple[str, ...],
    collapsed: bool,
    thresholds: ProbeThresholds,
) -> tuple[tuple[str, ...], int]:
    sequences = [sequence_of(trajectory, collapse=collapsed) for trajectory in trajectories]
    needed = _at_least(len(sequences), thresholds.prefix_support_per_mille)
    for length in range(len(modal), 0, -1):
        candidate = modal[:length]
        shared = sum(1 for sequence in sequences if sequence[:length] == candidate)
        if shared >= needed:
            return candidate, shared
    return (), 0


def _verdict(
    eligible: int,
    modal_count: int,
    prefix: tuple[str, ...],
    prefix_count: int,
    thresholds: ProbeThresholds,
) -> CompilabilityVerdict:
    if eligible < thresholds.min_eligible:
        return CompilabilityVerdict.INSUFFICIENT_EVIDENCE
    if modal_count >= _at_least(eligible, thresholds.compilable_support_per_mille):
        return CompilabilityVerdict.COMPILABLE
    reaches_floor = modal_count >= _at_least(eligible, thresholds.floor_support_per_mille)
    keeps_prefix = bool(prefix) and prefix_count >= _at_least(
        eligible, thresholds.prefix_support_per_mille
    )
    if reaches_floor and keeps_prefix:
        return CompilabilityVerdict.PREFIX_ONLY
    return CompilabilityVerdict.NON_COMPILABLE


def _at_least(total: int, per_mille: int) -> int:
    return -((-total * per_mille) // 1000)


def _empty(category: ExceptionCategory, thresholds: ProbeThresholds) -> CategoryProbe:
    del thresholds
    return CategoryProbe(
        category=category,
        eligible=0,
        collapsed=False,
        modal_sequence=(),
        modal_count=0,
        prefix=(),
        prefix_count=0,
        alternatives=(),
        verdict=CompilabilityVerdict.INSUFFICIENT_EVIDENCE,
    )


def _single_model(trajectories: Sequence[Trajectory]) -> str:
    # a skeleton computed across two models conflates them; SS I.8 compares separate runs instead
    models = sorted({trajectory.agent_model_id for trajectory in trajectories})
    if not models:
        raise CompilerError("no trajectories were supplied to the probe")
    if len(models) > 1:
        raise CompilerError(f"the probe refuses mixed producing models: {models}")
    return models[0]


def _require_categories(
    eligible: Sequence[Trajectory], category_of: Mapping[UUID, ExceptionCategory]
) -> None:
    missing = [t.correlation_id for t in eligible if t.trajectory_id not in category_of]
    if missing:
        raise CompilerError(f"no category was supplied for {len(missing)} eligible trajectories")
