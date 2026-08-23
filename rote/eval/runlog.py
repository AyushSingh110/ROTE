from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

RecordT = TypeVar("RecordT", bound=BaseModel)


def write_records(path: Path, records: Iterable[BaseModel]) -> int:
    written = 0
    with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")
            written += 1
    return written


# every line is validated: a log that cannot be parsed is not evidence, so this raises
# rather than skipping the line and quietly reporting a smaller denominator
def read_records(path: Path, model: type[RecordT]) -> list[RecordT]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(model.model_validate_json(line))
    return records


__all__ = ["read_records", "write_records"]
