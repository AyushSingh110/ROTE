from __future__ import annotations

from typing import Any

DEFAULT_MAX_DEPTH = 6


def enumerate_paths(value: Any, *, max_depth: int = DEFAULT_MAX_DEPTH) -> dict[str, Any]:
    found: dict[str, Any] = {}
    _walk(value, "$", 0, max_depth, found)
    return found


def _walk(value: Any, path: str, depth: int, max_depth: int, found: dict[str, Any]) -> None:
    if depth > max_depth:
        return
    found[path] = value
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _walk(item, f"{path}.{key}", depth + 1, max_depth, found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", depth + 1, max_depth, found)


def resolve_path(path: str, root: Any) -> tuple[bool, Any]:
    found = enumerate_paths(root)
    if path not in found:
        return False, None
    return True, found[path]


# shallowest first, then alphabetical, so a tie is broken the same way on every run
def rank_path(path: str) -> tuple[int, str]:
    return path.count(".") + path.count("["), path
