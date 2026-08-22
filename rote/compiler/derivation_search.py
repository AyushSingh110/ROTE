from __future__ import annotations

from collections.abc import Sequence
from itertools import permutations
from typing import Any, NamedTuple

from rote.compiler.derivations import DERIVATIONS, SEARCH_ORDER
from rote.compiler.paths import enumerate_paths, rank_path
from rote.contracts.plan import BindingKind, DerivationCandidate, DerivationOperand

MAX_OPERANDS = 24
MAX_ALTERNATIVES = 5


class _Operand(NamedTuple):
    step_index: int | None
    json_path: str

    def to_contract(self) -> DerivationOperand:
        if self.step_index is None:
            return DerivationOperand(kind=BindingKind.FROM_INPUT, json_path=self.json_path)
        return DerivationOperand(
            kind=BindingKind.FROM_STEP,
            json_path=self.json_path,
            source_step_index=self.step_index,
        )


def search_derivations(
    observed: Sequence[Any],
    task_inputs: Sequence[dict[str, Any]],
    prior_results: Sequence[Sequence[dict[str, Any]]],
) -> tuple[DerivationCandidate, ...]:
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in observed):
        return ()

    per_run = [
        _integer_operands(task_input, prior)
        for task_input, prior in zip(task_inputs, prior_results, strict=True)
    ]
    shared = set(per_run[0])
    for operands in per_run[1:]:
        shared &= set(operands)
    ordered = sorted(
        shared, key=lambda o: (o.step_index is not None, o.step_index or 0, rank_path(o.json_path))
    )[:MAX_OPERANDS]
    if not ordered:
        return ()

    hits: list[DerivationCandidate] = []
    for name in SEARCH_ORDER:
        arity = DERIVATIONS[name].arity
        apply = DERIVATIONS[name].apply
        for combination in permutations(ordered, arity):
            # check one run first: almost every combination dies here, so the rest stays cheap
            if apply(*(per_run[0][operand] for operand in combination)) != observed[0]:
                continue
            if all(
                apply(*(operands[operand] for operand in combination)) == want
                for operands, want in zip(per_run[1:], observed[1:], strict=True)
            ):
                hits.append(
                    DerivationCandidate(
                        derivation_id=name,
                        operands=tuple(operand.to_contract() for operand in combination),
                    )
                )
                if len(hits) > MAX_ALTERNATIVES:
                    return tuple(hits)
    return tuple(hits)


def _integer_operands(
    task_input: dict[str, Any], prior_results: Sequence[dict[str, Any]]
) -> dict[_Operand, int]:
    found: dict[_Operand, int] = {}
    for path, value in enumerate_paths(task_input).items():
        if isinstance(value, int) and not isinstance(value, bool):
            found[_Operand(None, path)] = value
    for index, result in enumerate(prior_results):
        for path, value in enumerate_paths(result).items():
            if isinstance(value, int) and not isinstance(value, bool):
                found[_Operand(index, path)] = value
    return found
