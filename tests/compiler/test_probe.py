import ast
import pathlib
from collections.abc import Sequence

import pytest

from rote.compiler.probe import (
    CategoryProbe,
    CompilabilityVerdict,
    ProbeReport,
    ProbeThresholds,
    format_probe_report,
    research_grade,
    run_probe,
)
from rote.compiler.sequences import collapsed_sequence, group_by_sequence, tool_sequence
from rote.contracts.canonical import canonical_bytes
from rote.contracts.checker import CheckerVerdict
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.trajectory import Trajectory
from rote.domain.tools.registry import TOOL_NAMES
from tests.compiler.builders import ALPHA, DELTA, build, categories_all, population

COMPILER_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "compiler"
LOOSE = ProbeThresholds(
    compilable_support_per_mille=600,
    prefix_support_per_mille=600,
    floor_support_per_mille=300,
    min_eligible=10,
)


def probe(trajectories: Sequence[Trajectory], thresholds: ProbeThresholds = LOOSE) -> ProbeReport:
    return run_probe(
        trajectories,
        category_of=categories_all(trajectories),
        domain=Domain.RECONCILIATION,
        thresholds=thresholds,
    )


def only(report: ProbeReport) -> CategoryProbe:
    return report.categories[0]


class TestTheProbeAssumesNoToolSequence:
    def test_no_real_tool_name_appears_anywhere_in_the_compiler(self) -> None:
        for path in sorted(COMPILER_PACKAGE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for name in TOOL_NAMES:
                assert name not in source, f"{path.name} hardcodes the tool {name!r}"

    def test_the_compiler_imports_no_domain_tool_module(self) -> None:
        for path in sorted(COMPILER_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("rote.domain.tools")

    def test_it_discovers_a_skeleton_made_of_invented_tool_names(self) -> None:
        report = probe(population(ALPHA, 40))
        assert only(report).modal_sequence == ALPHA

    def test_it_reports_a_verdict_and_never_a_plan(self) -> None:
        report = probe(population(ALPHA, 40))
        for banned in ("plan", "steps", "bindings", "args"):
            assert banned not in set(type(only(report)).model_fields)


class TestSequenceExtraction:
    def test_the_sequence_is_the_tools_in_order(self) -> None:
        assert tool_sequence(build("a", ALPHA)) == ALPHA

    def test_collapsing_removes_consecutive_repeats(self) -> None:
        assert collapsed_sequence(build("a", ("alpha", "alpha", "beta"))) == ("alpha", "beta")

    def test_collapsing_drops_steps_that_errored(self) -> None:
        assert collapsed_sequence(build("a", ("alpha", "beta"), failed_at=0)) == ("beta",)

    def test_the_raw_sequence_keeps_repeats_and_errors(self) -> None:
        assert tool_sequence(build("a", ("alpha", "alpha"), failed_at=0)) == ("alpha", "alpha")

    def test_identical_sequences_are_grouped_together(self) -> None:
        groups = group_by_sequence(population(ALPHA, 7), collapse=False)
        assert len(groups) == 1
        assert groups[0].count == 7

    def test_groups_are_ordered_by_count_then_sequence(self) -> None:
        groups = group_by_sequence(population(ALPHA, 5, others=[(DELTA, 9)]), collapse=False)
        assert [group.count for group in groups] == [9, 5]

    def test_grouping_is_deterministic(self) -> None:
        built = population(ALPHA, 5, others=[(DELTA, 5)])
        first = [group.sequence for group in group_by_sequence(built, collapse=False)]
        second = [group.sequence for group in group_by_sequence(built, collapse=False)]
        assert first == second


class TestTheGoNoGoVerdict:
    def test_a_dominant_sequence_is_compilable(self) -> None:
        report = probe(population(ALPHA, 80, others=[(DELTA, 20)]))
        assert only(report).modal_support == pytest.approx(0.8)
        assert only(report).verdict is CompilabilityVerdict.COMPILABLE

    def test_a_split_population_falls_back_to_the_common_prefix(self) -> None:
        report = probe(
            population(
                ("alpha", "beta", "gamma"),
                50,
                others=[(("alpha", "beta", "delta"), 50)],
            )
        )
        result = only(report)
        assert result.verdict is CompilabilityVerdict.PREFIX_ONLY
        assert result.prefix == ("alpha", "beta")
        assert result.prefix_support == pytest.approx(1.0)

    def test_a_scattered_population_is_reported_non_compilable(self) -> None:
        scattered = [build(f"t{i}", (f"tool_{i}", "tail")) for i in range(40)]
        report = probe(scattered)
        assert only(report).verdict is CompilabilityVerdict.NON_COMPILABLE

    def test_a_scattered_population_lists_its_alternatives(self) -> None:
        scattered = [build(f"t{i}", (f"tool_{i}", "tail")) for i in range(40)]
        assert len(only(probe(scattered)).alternatives) >= 1

    def test_too_few_eligible_runs_is_insufficient_evidence_not_success(self) -> None:
        report = probe(population(ALPHA, 5))
        assert only(report).verdict is CompilabilityVerdict.INSUFFICIENT_EVIDENCE

    def test_insufficient_evidence_is_never_reported_as_compilable(self) -> None:
        report = probe(population(ALPHA, 9))
        assert only(report).verdict is not CompilabilityVerdict.COMPILABLE

    def test_the_thresholds_used_are_recorded_in_the_report(self) -> None:
        report = probe(population(ALPHA, 40))
        assert report.thresholds == LOOSE

    def test_collapsing_is_used_when_it_raises_support(self) -> None:
        noisy = [build(f"r{i}", ("alpha", "alpha", "beta")) for i in range(20)]
        clean = [build(f"c{i}", ("alpha", "beta")) for i in range(20)]
        result = only(probe(noisy + clean))
        assert result.collapsed is True
        assert result.modal_sequence == ("alpha", "beta")
        assert result.modal_support == pytest.approx(1.0)


class TestEligibilityIsEnforcedByTheProbe:
    def test_unverified_runs_never_reach_the_probe(self) -> None:
        built = population(ALPHA, 40) + [
            build(f"bad{i}", DELTA, verdict=CheckerVerdict.FAIL) for i in range(40)
        ]
        report = probe(built)
        assert only(report).eligible == 40
        assert only(report).modal_sequence == ALPHA

    def test_the_report_carries_the_eligibility_breakdown(self) -> None:
        built = population(ALPHA, 40) + [
            build(f"bad{i}", DELTA, verdict=CheckerVerdict.FAIL) for i in range(10)
        ]
        report = probe(built)
        assert report.eligibility.examined == 50
        assert report.eligibility.eligible == 40

    def test_a_category_with_no_eligible_runs_still_appears(self) -> None:
        built = [build(f"bad{i}", ALPHA, verdict=CheckerVerdict.FAIL) for i in range(10)]
        report = probe(built)
        assert only(report).eligible == 0
        assert only(report).verdict is CompilabilityVerdict.INSUFFICIENT_EVIDENCE


class TestModelProvenance:
    def test_a_report_carries_the_model_that_produced_its_trajectories(self) -> None:
        report = probe(population(ALPHA, 40, model="some-real-model"))
        assert report.agent_model_id == "some-real-model"

    def test_mixing_two_models_in_one_probe_is_refused(self) -> None:
        mixed = population(ALPHA, 20, model="model-a") + population(ALPHA, 20, model="model-b")
        with pytest.raises(CompilerError):
            probe(mixed)

    def test_a_report_from_the_offline_test_double_is_not_research_grade(self) -> None:
        report = probe(population(ALPHA, 40, model="offline-heuristic-1"))
        assert (
            research_grade(report, test_double_model_ids=frozenset({"offline-heuristic-1"}))
            is False
        )

    def test_a_report_from_a_real_model_is_research_grade(self) -> None:
        report = probe(population(ALPHA, 40, model="some-real-model"))
        assert (
            research_grade(report, test_double_model_ids=frozenset({"offline-heuristic-1"})) is True
        )


class TestReportShape:
    def test_categories_are_reported_in_a_stable_order(self) -> None:
        built = population(ALPHA, 40)
        mapping = {
            trajectory.trajectory_id: (
                ExceptionCategory.FEE_MISMATCH if index % 2 else ExceptionCategory.TIMING_CUTOFF
            )
            for index, trajectory in enumerate(built)
        }
        report = run_probe(
            built, category_of=mapping, domain=Domain.RECONCILIATION, thresholds=LOOSE
        )
        assert [result.category for result in report.categories] == sorted(
            {result.category for result in report.categories}
        )

    def test_the_report_is_deterministic(self) -> None:
        built = population(ALPHA, 40, others=[(DELTA, 10)])
        first = canonical_bytes(probe(built).model_dump(mode="json"))
        second = canonical_bytes(probe(built).model_dump(mode="json"))
        assert first == second

    def test_a_trajectory_without_a_category_is_rejected(self) -> None:
        built = population(ALPHA, 20)
        with pytest.raises(CompilerError):
            run_probe(built, category_of={}, domain=Domain.RECONCILIATION, thresholds=LOOSE)

    def test_the_rendered_table_names_every_required_column(self) -> None:
        rendered = format_probe_report(probe(population(ALPHA, 40)))
        for heading in ("eligible", "support", "prefix", "verdict", "modal"):
            assert heading in rendered.lower()

    def test_the_rendered_table_states_the_producing_model(self) -> None:
        rendered = format_probe_report(probe(population(ALPHA, 40, model="some-real-model")))
        assert "some-real-model" in rendered
