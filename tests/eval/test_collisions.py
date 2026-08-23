import ast
import pathlib

import pytest

from rote.contracts.common import ExceptionCategory, UntrustedText
from rote.eval.collisions import (
    COLLISION_BAND,
    CollisionCase,
    is_collision,
    note_lines,
)

EXPERIMENT_MODULES = ("fee_rule.py", "margin.py", "stability.py", "collisions.py")
EVAL_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "eval"
PRODUCTION = ("rote.compiler", "rote.runtime", "rote.agent", "rote.safety", "rote.recorder")


def case(**overrides: object) -> CollisionCase:
    fields: dict[str, object] = {
        "seed": 5,
        "size": 5000,
        "exception_id": "EXC-1",
        "true_category": ExceptionCategory.PARTIAL_PAYMENT,
        "d_floor": 0,
        "d_half_up": 1,
        "internal_minor_units": 100_000,
        "bank_minor_units": 97_000,
        "shortfall": 3_000,
        "expected_fee_floor": 3_000,
        "expected_fee_half_up": 3_001,
        "notes": (("$.merchant_note", "customer paid half now"),),
    }
    fields.update(overrides)
    return CollisionCase.model_validate(fields)


class TestTheCollisionBandIsTheMeasuredOne:
    # the band is the measured effect of the rounding convention, not a chosen tolerance
    def test_it_is_one_minor_unit(self) -> None:
        assert COLLISION_BAND == 1


class TestWhichCasesCount:
    @pytest.mark.parametrize(("floor", "half_up"), [(0, 0), (1, 1), (0, 900), (900, 1)])
    def test_being_inside_the_band_under_either_rounding_is_a_collision(
        self, floor: int, half_up: int
    ) -> None:
        assert is_collision(floor, half_up) is True

    def test_sitting_outside_the_band_under_both_is_not(self) -> None:
        assert is_collision(2, 2) is False

    def test_a_distance_that_could_not_be_computed_does_not_count_as_close(self) -> None:
        assert is_collision(None, None) is False
        assert is_collision(None, 900) is False

    # an abstention on one side must not hide a collision on the other
    def test_one_computable_distance_inside_the_band_is_enough(self) -> None:
        assert is_collision(None, 1) is True


class TestTheNotesAreReportedVerbatim:
    def test_every_note_is_carried_with_its_path(self) -> None:
        blocks = (
            UntrustedText.of("$.merchant_note", "paid part of it"),
            UntrustedText.of("$.bank_memo", "fee deducted"),
        )
        assert note_lines(blocks) == (
            ("$.merchant_note", "paid part of it"),
            ("$.bank_memo", "fee deducted"),
        )

    def test_a_case_with_no_note_is_recorded_as_having_none(self) -> None:
        assert note_lines(()) == ()
        assert case(notes=()).notes == ()

    def test_the_text_is_not_altered_in_any_way(self) -> None:
        raw = "  Customer PAID  half   now!!  \n"
        assert note_lines((UntrustedText.of("$.merchant_note", raw),)) == (
            ("$.merchant_note", raw),
        )

    # this experiment reports evidence; it must not decide anything
    def test_the_module_holds_no_keywords_and_no_verdict(self) -> None:
        source = (EVAL_PACKAGE / "collisions.py").read_text(encoding="utf-8").lower()
        # "shortfall" is deliberately not banned: it names the arithmetic quantity
        # internal - bank, not a phrase matched against a note
        for banned in ("partial payment", "paid half", "keyword", "score", "predict", "classif"):
            assert banned not in source


class TestTheExperimentStaysInsideItsBoundary:
    def test_no_experiment_module_imports_production_code(self) -> None:
        for name in EXPERIMENT_MODULES:
            tree = ast.parse((EVAL_PACKAGE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(PRODUCTION), f"{name} imports {node.module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith(PRODUCTION), f"{name} imports {alias.name}"
