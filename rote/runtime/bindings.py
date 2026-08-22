from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rote.contracts.derivations import apply_derivation
from rote.contracts.errors import ExecutorError
from rote.contracts.paths import resolve_path
from rote.contracts.plan import ArgBinding, BindingKind, DerivationCandidate


def resolve_binding(
    binding: ArgBinding, task_input: dict[str, Any], committed: Sequence[dict[str, Any]]
) -> tuple[bool, Any]:
    if binding.kind is BindingKind.LITERAL:
        return True, binding.literal_value
    if binding.kind is BindingKind.FROM_INPUT:
        return resolve_path(str(binding.json_path), task_input)
    if binding.kind is BindingKind.FROM_STEP:
        index = binding.source_step_index
        if index is None or index >= len(committed):
            return False, None
        return resolve_path(str(binding.json_path), committed[index])
    if binding.kind is BindingKind.FROM_DERIVATION:
        return _resolve_derivation(binding.derivation, task_input, committed)
    raise ExecutorError(f"binding kind {binding.kind.value} cannot be executed in v1")


def _resolve_derivation(
    derivation: DerivationCandidate | None,
    task_input: dict[str, Any],
    committed: Sequence[dict[str, Any]],
) -> tuple[bool, Any]:
    if derivation is None:
        return False, None
    values: list[int] = []
    for operand in derivation.operands:
        if operand.kind is BindingKind.FROM_INPUT:
            found, value = resolve_path(operand.json_path, task_input)
        elif operand.source_step_index is None or operand.source_step_index >= len(committed):
            return False, None
        else:
            found, value = resolve_path(operand.json_path, committed[operand.source_step_index])
        if not found or not isinstance(value, int) or isinstance(value, bool):
            return False, None
        values.append(value)
    return True, apply_derivation(derivation.derivation_id, values)
