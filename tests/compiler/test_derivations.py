import ast
import pathlib
from collections.abc import Sequence

import pytest

from rote.compiler.builder import build_plan
from rote.compiler.derivations import DERIVATIONS, apply_derivation
from rote.compiler.replay import replay_plan, validate_plan
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.errors import CompilerError
from rote.contracts.plan import BindingKind, Plan, PolicyRequirement
from rote.contracts.trajectory import Trajectory
from tests.compiler.builders import build_with_steps

COMPILER_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "compiler"
POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha", "beta"}), max_per_action={Currency.INR: 50_000}
)


def compile_from(runs: Sequence[Trajectory]) -> Plan:
    return build_plan(
        runs,
        domain=Domain.RECONCILIATION,
        category=ExceptionCategory.FEE_MISMATCH,
        policy=POLICY,
    )


class TestTheRegistryIsClosedAndInert:
    def test_every_entry_is_named_and_declares_its_arity(self) -> None:
        for name, derivation in DERIVATIONS.items():
            assert name
            assert derivation.arity >= 2

    def test_an_unknown_formula_name_is_refused(self) -> None:
        with pytest.raises(CompilerError):
            apply_derivation("definitely_not_a_formula", [1, 2])

    def test_the_wrong_number_of_operands_is_refused(self) -> None:
        with pytest.raises(CompilerError):
            apply_derivation("difference", [1, 2, 3])

    def test_the_compiler_never_evaluates_a_string(self) -> None:
        banned = {"eval", "exec", "compile", "literal_eval"}
        for path in sorted(COMPILER_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in banned, f"{path.name} calls {node.func.id}"

    def test_the_formulas_are_plain_integer_arithmetic(self) -> None:
        assert apply_derivation("difference", [10, 4]) == 6
        assert apply_derivation("sum", [10, 4]) == 14
        assert apply_derivation("scaled_difference", [10, 4, 2_000_000]) == 16


class TestDerivedArgumentsBind:
    def test_a_subtraction_of_two_task_fields_binds_to_a_formula(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": (100 + i) - (40 + i * 2)}, {"ok": 1})],
                task_input={"internal": 100 + i, "bank": 40 + i * 2},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[0].args[0]
        assert binding.kind is BindingKind.FROM_DERIVATION
        assert binding.derivation is not None
        assert binding.derivation.derivation_id == "difference"

    def test_the_operands_name_the_fields_they_came_from(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": (100 + i) - (40 + i * 2)}, {"ok": 1})],
                task_input={"internal": 100 + i, "bank": 40 + i * 2},
            )
            for i in range(20)
        ]
        derivation = compile_from(runs).steps[0].args[0].derivation
        assert derivation is not None
        assert [operand.json_path for operand in derivation.operands] == [
            "$.internal",
            "$.bank",
        ]

    def test_a_formula_may_use_a_value_produced_by_an_earlier_step(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"k": 1}, {"rate_micros": 2_000_000}),
                    (
                        "beta",
                        {"amount": ((100 + i) * 2_000_000 // 1_000_000) - (40 + i)},
                        {"ok": 1},
                    ),
                ],
                task_input={"internal": 100 + i, "bank": 40 + i},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[1].args[0]
        assert binding.kind is BindingKind.FROM_DERIVATION
        assert binding.derivation is not None
        assert binding.derivation.derivation_id == "scaled_difference"
        assert any(operand.kind is BindingKind.FROM_STEP for operand in binding.derivation.operands)

    def test_the_simplest_formula_wins_when_several_fit(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [
                    ("alpha", {"k": 1}, {"rate_micros": 1_000_000}),
                    ("beta", {"amount": i}, {"ok": 1}),
                ],
                task_input={"internal": 10 + i, "bank": 10},
            )
            for i in range(20)
        ]
        derivation = compile_from(runs).steps[1].args[0].derivation
        assert derivation is not None
        assert len(derivation.operands) == 2

    def test_competing_formulas_are_recorded_as_alternatives(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": (100 + i) - (40 + i * 2)}, {"ok": 1})],
                task_input={"internal": 100 + i, "copy": 100 + i, "bank": 40 + i * 2},
            )
            for i in range(20)
        ]
        binding = compile_from(runs).steps[0].args[0]
        assert binding.alternative_derivations

    def test_a_value_no_formula_explains_still_truncates_the_plan(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": i * i * 7 + 13}, {"ok": 1})],
                task_input={"internal": 100 + i, "bank": 40 + i},
            )
            for i in range(20)
        ]
        plan = compile_from(runs)
        assert plan.truncated is True
        assert plan.steps == ()

    def test_a_simpler_binding_is_always_preferred_over_a_formula(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": 100 + i}, {"ok": 1})],
                task_input={"internal": 100 + i, "bank": 0},
            )
            for i in range(20)
        ]
        assert compile_from(runs).steps[0].args[0].kind is BindingKind.FROM_INPUT

    def test_binding_a_formula_is_deterministic(self) -> None:
        runs = [
            build_with_steps(
                f"t{i}",
                [("alpha", {"amount": (100 + i) - (40 + i * 2)}, {"ok": 1})],
                task_input={"internal": 100 + i, "bank": 40 + i * 2, "spare": i},
            )
            for i in range(20)
        ]
        first = compile_from(runs).steps[0].args[0]
        second = compile_from(runs).steps[0].args[0]
        assert first.model_dump() == second.model_dump()


class TestDerivedArgumentsReplay:
    def _runs(self, count: int, offset: int = 0) -> list[Trajectory]:
        # the difference must vary, or it would bind as a constant and prove nothing
        return [
            build_with_steps(
                f"t{offset + i}",
                [("alpha", {"amount": (100 + (offset + i) * 3) - (40 + offset + i)}, {"ok": 1})],
                task_input={"internal": 100 + (offset + i) * 3, "bank": 40 + offset + i},
            )
            for i in range(count)
        ]

    def test_a_derived_argument_is_recomputed_during_replay(self) -> None:
        plan = compile_from(self._runs(20))
        outcome = replay_plan(plan, self._runs(1, offset=500)[0])
        assert outcome.path_equal is True
        assert outcome.playback_miss is False

    def test_a_holdout_of_unseen_runs_validates(self) -> None:
        plan = compile_from(self._runs(20))
        report = validate_plan(plan, self._runs(10, offset=500))
        assert report.path_equal == 10
        assert report.passed is True

    def test_a_run_whose_arithmetic_differs_is_a_playback_miss(self) -> None:
        plan = compile_from(self._runs(20))
        odd = build_with_steps(
            "odd",
            [("alpha", {"amount": 999_999}, {"ok": 1})],
            task_input={"internal": 100, "bank": 40},
        )
        outcome = replay_plan(plan, odd)
        assert outcome.playback_miss is True
