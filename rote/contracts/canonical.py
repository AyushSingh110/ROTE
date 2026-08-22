from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypeAlias

from rote.contracts.errors import CanonicalisationError

JsonValue: TypeAlias = "None | bool | int | str | list[JsonValue] | dict[str, JsonValue]"

MAX_NESTING_DEPTH = 64


def canonical_bytes(value: Any) -> bytes:
    _reject_uncanonicalisable(value, path="$", depth=0)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_iso8601(moment: datetime) -> str:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise CanonicalisationError("naive datetime has no single canonical rendering")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# accepts Any because this is the boundary that decides what is canonicalisable at all
def _reject_uncanonicalisable(value: Any, *, path: str, depth: int) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise CanonicalisationError(f"{path}: nesting deeper than {MAX_NESTING_DEPTH}")
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_uncanonicalisable(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalisationError(f"{path}: dict key {key!r} is not a string")
            _reject_uncanonicalisable(item, path=f"{path}.{key}", depth=depth + 1)
        return
    # floats land here on purpose: money is integer minor units and rates are scaled integers
    raise CanonicalisationError(f"{path}: {type(value).__name__} is not canonically serialisable")
