from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from rote.compiler.derivation_search import search_derivations
from rote.contracts.paths import enumerate_paths, rank_path
from rote.contracts.plan import ArgBinding, BindingKind


class PathMatch(NamedTuple):
    best: str
    alternatives: tuple[str, ...]


def infer_binding(
    arg_name: str,
    observed: Sequence[Any],
    task_inputs: Sequence[dict[str, Any]],
    prior_results: Sequence[Sequence[dict[str, Any]]],
) -> ArgBinding | None:
    if _all_identical(observed):
        return ArgBinding(
            arg_name=arg_name,
            kind=BindingKind.LITERAL,
            literal_value=observed[0],
            evidence_run_count=len(observed),
        )

    from_input = _matching_paths(observed, [enumerate_paths(t) for t in task_inputs])
    if from_input is not None:
        return ArgBinding(
            arg_name=arg_name,
            kind=BindingKind.FROM_INPUT,
            json_path=from_input.best,
            evidence_run_count=len(observed),
            alternative_paths=from_input.alternatives,
        )

    # earliest producing step wins, so a plan takes the shortest data dependency it can
    for step_index in range(len(prior_results[0])):
        candidates = [run[step_index] for run in prior_results]
        from_step = _matching_paths(observed, [enumerate_paths(c) for c in candidates])
        if from_step is not None:
            return ArgBinding(
                arg_name=arg_name,
                kind=BindingKind.FROM_STEP,
                json_path=from_step.best,
                source_step_index=step_index,
                evidence_run_count=len(observed),
                alternative_paths=from_step.alternatives,
            )

    # last resort before giving up: a named formula over typed integer fields, never a model
    derivations = search_derivations(observed, task_inputs, prior_results)
    if derivations:
        return ArgBinding(
            arg_name=arg_name,
            kind=BindingKind.FROM_DERIVATION,
            derivation=derivations[0],
            evidence_run_count=len(observed),
            alternative_derivations=derivations[1:],
        )
    return None


def _all_identical(observed: Sequence[Any]) -> bool:
    first = observed[0]
    return all(_same(value, first) for value in observed[1:])


def _matching_paths(
    observed: Sequence[Any], per_run_paths: Sequence[dict[str, Any]]
) -> PathMatch | None:
    shared: set[str] | None = None
    for value, paths in zip(observed, per_run_paths, strict=True):
        matching = {path for path, found in paths.items() if _same(found, value)}
        shared = matching if shared is None else shared & matching
        if not shared:
            return None
    if not shared:
        return None
    ordered = sorted(shared, key=rank_path)
    return PathMatch(best=ordered[0], alternatives=tuple(ordered[1:]))


# exact type match as well as equality, so an int argument never binds to a look-alike string
def _same(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right
