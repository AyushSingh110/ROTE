from collections.abc import Sequence

import pytest

from rote.compiler.builder import COMPILER_VERSION, build_plan
from rote.compiler.paths import enumerate_paths
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.plan import (
    BindingKind,
    Plan,
    PlanStatus,
    PolicyRequirement,
    TruncationReason,
)
from rote.contracts.trajectory import Trajectory
from tests.compiler.builders import build_with_steps

POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha", "beta", "gamma"}),
    max_per_action={Currency.INR: 50_000},
)


def compile_from(runs: Sequence[Trajectory]) -> Plan:
    return build_plan(
        runs,
        domain=Domain.RECONCILIATION,
        category=ExceptionCategory.FEE_MISMATCH,
        policy=POLICY,
    )


class TestJsonPaths:
    def test_a_flat_object_yields_dotted_paths(self) -> None:
        paths = enumerate_paths({"a": 1, "b": "x"})
        assert paths["$.a"] == 1
        assert paths["$.b"] == "x"

    def test_nested_objects_are_reachable(self) -> None:
        assert enumerate_paths({"o": {"i": 5}})["$.o.i"] == 5

    def test_list_elements_are_indexed(self) -> None:
        assert enumerate_paths({"xs": ["a", "b"]})["$.xs[1]"] == "b"

    def test_containers_are_addressable_too(self) -> None:
        assert enumerate_paths({"o": {"i": 5}})["$.o"] == {"i": 5}


class TestLiteralBinding:
    def test_an_argument_identical_in_every_run_becomes_a_literal(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"window": 7}, {"ok": 1})],
                task_input={"record_id": f"REC-{i}"},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        binding = plan.steps[0].args[0]
        assert binding.kind is BindingKind.LITERAL
        assert binding.literal_value == 7
        assert binding.evidence_run_count == 20


class TestFromInputBinding:
    def test_an_argument_that_always_equals_a_task_field_binds_to_it(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"record_id": f"REC-{i}"}, {"ok": 1})],
                task_input={"record_id": f"REC-{i}", "other": "constant"},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[0].args[0]
        assert binding.kind is BindingKind.FROM_INPUT
        assert binding.json_path == "$.record_id"

    def test_a_nested_task_field_is_found(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"cur": "INR"}, {"ok": 1})],
                task_input={"amount": {"cur": "INR", "minor": i}},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[0].args[0]
        assert binding.kind is BindingKind.LITERAL

    def test_ambiguous_paths_are_recorded_rather_than_silently_resolved(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"rid": f"REC-{i}"}, {"ok": 1})],
                task_input={"a": f"REC-{i}", "b": f"REC-{i}"},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[0].args[0]
        assert binding.kind is BindingKind.FROM_INPUT
        assert binding.alternative_paths

    def test_a_value_of_a_different_type_never_binds(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"n": i}, {"ok": 1})],
                task_input={"n": str(i)},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.truncated is True
        assert plan.steps == ()


class TestFromStepBinding:
    def test_an_argument_taken_from_an_earlier_result_binds_to_that_step(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"record_id": f"REC-{i}"}, {"line_id": f"BNK-{i}"}),
                    ("beta", {"line_id": f"BNK-{i}"}, {"ok": 1}),
                ],
                task_input={"record_id": f"REC-{i}"},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[1].args[0]
        assert binding.kind is BindingKind.FROM_STEP
        assert binding.source_step_index == 0
        assert binding.json_path == "$.line_id"

    def test_the_earliest_producing_step_is_preferred(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"r": f"R{i}"}, {"v": f"V{i}"}),
                    ("beta", {"r": f"R{i}"}, {"v": f"V{i}"}),
                    ("gamma", {"v": f"V{i}"}, {"ok": 1}),
                ],
                task_input={"r": f"R{i}"},
            )
            for i in range(20)
        ]
        assert compile_from(runs).steps[2].args[0].source_step_index == 0


class TestTruncation:
    def test_a_derived_value_that_binds_to_nothing_truncates_the_plan(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"record_id": f"REC-{i}"}, {"ok": 1}),
                    ("beta", {"total": i * 3 + 11}, {"ok": 1}),
                ],
                task_input={"record_id": f"REC-{i}", "a": i, "b": i},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.truncated is True
        assert plan.truncation_reason is TruncationReason.UNBOUND_ARGUMENT
        assert len(plan.steps) == 1

    def test_a_truncated_plan_still_compiles_the_prefix(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"record_id": f"REC-{i}"}, {"ok": 1}),
                    ("beta", {"derived": i * 7 + 3}, {"ok": 1}),
                ],
                task_input={"record_id": f"REC-{i}"},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert [step.tool for step in plan.steps] == ["alpha"]

    def test_differing_argument_names_truncate_rather_than_guess(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"a": 1} if i % 2 else {"b": 1}, {"ok": 1})],
                task_input={"x": 1},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.truncated is True
        assert plan.truncation_reason is TruncationReason.INCONSISTENT_ARGUMENTS

    def test_a_fully_bound_plan_is_not_truncated(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"record_id": f"REC-{i}", "window": 7}, {"ok": 1})],
                task_input={"record_id": f"REC-{i}"},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.truncated is False
        assert plan.truncation_reason is None


class TestExpectations:
    def test_the_learned_fingerprints_come_from_the_observed_results(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"amount": i})], task_input={"x": 1})
            for i in range(20)
        ]
        expectation = compile_from(runs).steps[0].expect
        assert len(expectation.result_fingerprints) == 1
        assert expectation.sample_count == 20

    def test_numeric_ranges_record_what_was_actually_seen(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"amount": i})], task_input={"x": 1})
            for i in range(20)
        ]
        observed = compile_from(runs).steps[0].expect.numeric_observed
        assert observed["$.amount"] == (0, 19)

    def test_the_widened_range_is_never_narrower_than_what_was_seen(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}", [("alpha", {"w": 1}, {"amount": i * 10})], task_input={"x": 1}
            )
            for i in range(20)
        ]
        expectation = compile_from(runs).steps[0].expect
        low, high = expectation.numeric_observed["$.amount"]
        wide_low, wide_high = expectation.numeric_widened["$.amount"]
        assert wide_low <= low
        assert wide_high >= high

    def test_both_the_raw_and_widened_ranges_are_kept(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"amount": i})], task_input={"x": 1})
            for i in range(20)
        ]
        expectation = compile_from(runs).steps[0].expect
        assert set(expectation.numeric_observed) == set(expectation.numeric_widened)

    def test_a_small_value_set_is_learned_as_a_categorical_domain(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"w": 1}, {"status": "ok" if i % 2 else "late"})],
                task_input={"x": 1},
            )
            for i in range(20)
        ]
        domains = compile_from(runs).steps[0].expect.categorical_domains
        assert domains["$.status"] == frozenset({"ok", "late"})

    def test_a_high_cardinality_field_is_not_treated_as_categorical(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ref": f"R{i}"})], task_input={"x": 1})
            for i in range(20)
        ]
        assert "$.ref" not in compile_from(runs).steps[0].expect.categorical_domains

    def test_booleans_are_never_treated_as_numbers(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"voided": False})], task_input={"x": 1})
            for i in range(20)
        ]
        assert "$.voided" not in compile_from(runs).steps[0].expect.numeric_observed


class TestPlanShape:
    def test_a_new_plan_is_emitted_as_a_draft(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.status is PlanStatus.DRAFT
        assert plan.validation is None

    def test_the_plan_records_which_runs_taught_it(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert len(plan.built_from) == 20

    def test_the_plan_records_the_producing_model_and_compiler_version(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.agent_model_id == "some-real-model"
        assert plan.compiler_version == COMPILER_VERSION

    def test_the_plan_reports_its_own_coverage(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(15)
        ] + [
            build_with_steps(f"o{i}", [("beta", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(5)
        ]
        plan = compile_from(runs)
        assert plan.coverage_count == 15
        assert plan.coverage_total == 20

    def test_only_the_modal_group_teaches_the_plan(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(15)
        ] + [
            build_with_steps(f"o{i}", [("beta", {"w": 2}, {"ok": 1})], task_input={"x": 1})
            for i in range(5)
        ]
        plan = compile_from(runs)
        assert plan.skeleton == ("alpha",)
        assert plan.steps[0].args[0].evidence_run_count == 15

    def test_mixing_models_is_refused(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1}, model="a"
            )
            for i in range(10)
        ] + [
            build_with_steps(
                f"o{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1}, model="b"
            )
            for i in range(10)
        ]
        with pytest.raises(CompilerError):
            compile_from(runs)

    def test_compiling_from_nothing_is_refused(self) -> None:
        with pytest.raises(CompilerError):
            compile_from([])

    def test_the_plan_carries_no_executable_string(self) -> None:
        runs = [
            build_with_steps(f"t{i}", [("alpha", {"w": 1}, {"ok": 1})], task_input={"x": 1})
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.steps[0].expect.invariants == ()
        for banned in ("expr", "expression", "code", "eval"):
            assert banned not in set(type(plan.steps[0].expect).model_fields)
