import ast
import pathlib
from collections.abc import Sequence

import pytest

from rote.compiler import derivation_search
from rote.compiler.builder import build_plan
from rote.compiler.derivation_search import search_derivations
from rote.compiler.replay import replay_plan
from rote.contracts.common import Currency, Domain, ExceptionCategory
from rote.contracts.derivations import DERIVATIONS, apply_derivation
from rote.contracts.errors import CompilerError
from rote.contracts.plan import (
    BindingKind,
    DerivationCandidate,
    DerivationOperand,
    Plan,
    PolicyRequirement,
)
from rote.contracts.trajectory import Trajectory
from tests.compiler.builders import build_with_steps

ROOT = pathlib.Path(__file__).resolve().parents[2] / "rote"
SCANNED = (
    *sorted((ROOT / "compiler").rglob("*.py")),
    ROOT / "contracts" / "derivations.py",
    ROOT / "contracts" / "paths.py",
    ROOT / "runtime" / "bindings.py",
)
POLICY = PolicyRequirement(
    allowed_tools=frozenset({"alpha"}), max_per_action={Currency.INR: 50_000}
)


def compile_from(runs: Sequence[Trajectory]) -> Plan:
    return build_plan(
        runs,
        domain=Domain.RECONCILIATION,
        category=ExceptionCategory.FEE_MISMATCH,
        policy=POLICY,
    )


def crowded_runs(count: int = 20) -> list[Trajectory]:
    # many equal integers, so several formulas fit and the alternatives list fills up
    return [
        build_with_steps(
            f"t{i}",
            [("alpha", {"amount": (100 + i * 3) - (40 + i)}, {"ok": 1})],
            task_input={
                "internal": 100 + i * 3,
                "internal_copy": 100 + i * 3,
                "internal_again": 100 + i * 3,
                "bank": 40 + i,
                "bank_copy": 40 + i,
            },
        )
        for i in range(count)
    ]


class TestUnknownFormulasFailClosed:
    def test_an_unknown_name_is_refused_by_the_registry(self) -> None:
        with pytest.raises(CompilerError):
            apply_derivation("wire_all_the_money", [1, 2])

    def test_a_plan_carrying_an_unknown_formula_refuses_to_replay(self) -> None:
        runs = crowded_runs()
        plan = compile_from(runs)
        forged = (
            plan.steps[0]
            .args[0]
            .model_copy(
                update={
                    "derivation": DerivationCandidate(
                        derivation_id="wire_all_the_money",
                        operands=(
                            DerivationOperand(kind=BindingKind.FROM_INPUT, json_path="$.internal"),
                            DerivationOperand(kind=BindingKind.FROM_INPUT, json_path="$.bank"),
                        ),
                    )
                }
            )
        )
        step = plan.steps[0].model_copy(update={"args": (forged,)})
        corrupt = plan.model_copy(update={"steps": (step,)})
        with pytest.raises(CompilerError):
            replay_plan(corrupt, runs[0])

    def test_an_unknown_name_never_falls_back_to_a_default(self) -> None:
        with pytest.raises(CompilerError):
            apply_derivation("", [1, 2])

    def test_the_compiler_never_looks_a_formula_up_dynamically(self) -> None:
        banned = {"getattr", "__import__", "globals", "locals", "vars", "setattr"}
        for path in SCANNED:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in banned, f"{path.name} calls {node.func.id}"

    def test_the_compiler_never_imports_a_module_by_name(self) -> None:
        for path in SCANNED:
            source = path.read_text(encoding="utf-8")
            assert "importlib" not in source

    def test_the_registry_is_a_fixed_mapping_of_named_entries(self) -> None:
        assert set(DERIVATIONS) == {
            "difference",
            "sum",
            "scaled_difference",
            "scaled_sum",
        }

    def test_the_search_only_ever_proposes_registered_names(self) -> None:
        runs = crowded_runs()
        candidates = search_derivations(
            [t.steps[0].args["amount"] for t in runs],
            [t.task_input_redacted for t in runs],
            [[] for _ in runs],
        )
        assert candidates
        assert all(c.derivation_id in DERIVATIONS for c in candidates)


class TestAlternativesDoNotChangeTheChoice:
    def _primary(self, limit: int, monkeypatch: pytest.MonkeyPatch) -> DerivationCandidate:
        monkeypatch.setattr(derivation_search, "MAX_ALTERNATIVES", limit)
        binding = compile_from(crowded_runs()).steps[0].args[0]
        assert binding.derivation is not None
        return binding.derivation

    def test_the_chosen_formula_is_the_same_however_many_alternatives_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen = [self._primary(limit, monkeypatch) for limit in (0, 1, 3, 5, 50)]
        assert all(candidate == chosen[0] for candidate in chosen)

    def test_keeping_no_alternatives_still_binds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(derivation_search, "MAX_ALTERNATIVES", 0)
        binding = compile_from(crowded_runs()).steps[0].args[0]
        assert binding.kind is BindingKind.FROM_DERIVATION

    def test_the_alternatives_list_never_exceeds_its_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(derivation_search, "MAX_ALTERNATIVES", 2)
        binding = compile_from(crowded_runs()).steps[0].args[0]
        assert len(binding.alternative_derivations) <= 2

    def test_the_chosen_formula_is_the_simplest_regardless_of_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(derivation_search, "MAX_ALTERNATIVES", 50)
        derivation = self._primary(50, monkeypatch)
        assert derivation.derivation_id == "difference"


class TestOperandCapRejectsRatherThanGuesses:
    def test_a_cap_below_the_smallest_arity_finds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(derivation_search, "MAX_OPERANDS", 1)
        runs = crowded_runs()
        candidates = search_derivations(
            [t.steps[0].args["amount"] for t in runs],
            [t.task_input_redacted for t in runs],
            [[] for _ in runs],
        )
        assert candidates == ()

    def test_a_cap_that_hides_the_true_operands_truncates_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(derivation_search, "MAX_OPERANDS", 1)
        plan = compile_from(crowded_runs())
        assert plan.truncated is True
        assert plan.steps == ()

    def test_whatever_survives_the_cap_still_reproduces_every_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = crowded_runs()
        observed = [t.steps[0].args["amount"] for t in runs]
        inputs = [t.task_input_redacted for t in runs]
        for cap in (2, 3, 5, 24):
            monkeypatch.setattr(derivation_search, "MAX_OPERANDS", cap)
            for candidate in search_derivations(observed, inputs, [[] for _ in runs]):
                for task_input, want in zip(inputs, observed, strict=True):
                    values = [
                        task_input[o.json_path.removeprefix("$.")] for o in candidate.operands
                    ]
                    assert apply_derivation(candidate.derivation_id, values) == want

    def test_a_smaller_cap_never_produces_a_formula_a_larger_cap_rejects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = crowded_runs()
        observed = [t.steps[0].args["amount"] for t in runs]
        inputs = [t.task_input_redacted for t in runs]

        monkeypatch.setattr(derivation_search, "MAX_OPERANDS", 24)
        wide = {
            (c.derivation_id, tuple(o.json_path for o in c.operands))
            for c in search_derivations(observed, inputs, [[] for _ in runs])
        }

        monkeypatch.setattr(derivation_search, "MAX_OPERANDS", 3)
        narrow = {
            (c.derivation_id, tuple(o.json_path for o in c.operands))
            for c in search_derivations(observed, inputs, [[] for _ in runs])
        }

        assert narrow <= wide
