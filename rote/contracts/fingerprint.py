from __future__ import annotations

import hashlib
from typing import Any

from rote.contracts.canonical import canonical_bytes
from rote.contracts.errors import FingerprintError

DEFAULT_MAX_DEPTH = 6

StructuralSchema = tuple[tuple[str, str], ...]


def structural_schema(value: Any, max_depth: int = DEFAULT_MAX_DEPTH) -> StructuralSchema:
    if max_depth < 1:
        raise FingerprintError("max_depth must be at least 1")
    pairs: set[tuple[str, str]] = set()
    _collect(value, path="$", depth=0, max_depth=max_depth, pairs=pairs)
    return tuple(sorted(pairs))


def structural_fingerprint(value: Any, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
    schema = structural_schema(value, max_depth=max_depth)
    return hashlib.sha256(canonical_bytes([[path, kind] for path, kind in schema])).hexdigest()


def _collect(
    value: Any, *, path: str, depth: int, max_depth: int, pairs: set[tuple[str, str]]
) -> None:
    if depth >= max_depth:
        pairs.add((path, "truncated"))
        return
    pairs.add((path, _type_name(value, path)))
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FingerprintError(f"{path}: dict key {key!r} is not a string")
            _collect(item, path=f"{path}.{key}", depth=depth + 1, max_depth=max_depth, pairs=pairs)
    elif isinstance(value, list):
        # every element shares one path so list length cannot affect the fingerprint
        for item in value:
            _collect(item, path=f"{path}[]", depth=depth + 1, max_depth=max_depth, pairs=pairs)


def _type_name(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    raise FingerprintError(f"{path}: {type(value).__name__} has no structural type name")
