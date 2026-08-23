from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from rote.contracts.common import ExceptionCategory, UntrustedText
from rote.eval.stability import ROUNDING_BAND

FROZEN = ConfigDict(extra="forbid", frozen=True)

# a case is a collision when the structured arithmetic cannot tell it apart: the band is the
# measured effect of the rounding convention, carried over unchanged from the margin experiment
COLLISION_BAND = ROUNDING_BAND


class CollisionCase(BaseModel):
    model_config = FROZEN

    seed: int
    size: int = Field(gt=0)
    exception_id: str = Field(min_length=1)
    true_category: ExceptionCategory
    d_floor: int | None
    d_half_up: int | None
    internal_minor_units: int
    bank_minor_units: int
    shortfall: int
    expected_fee_floor: int
    expected_fee_half_up: int
    # carried verbatim, path and all: this experiment reports evidence and decides nothing
    notes: tuple[tuple[str, str], ...]


def is_collision(d_floor: int | None, d_half_up: int | None, band: int = COLLISION_BAND) -> bool:
    return any(value is not None and value <= band for value in (d_floor, d_half_up))


def note_lines(blocks: Sequence[UntrustedText]) -> tuple[tuple[str, str], ...]:
    return tuple((block.source_path, block.content) for block in blocks)


__all__ = ["COLLISION_BAND", "CollisionCase", "is_collision", "note_lines"]
