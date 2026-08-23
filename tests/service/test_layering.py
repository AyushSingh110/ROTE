import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2] / "rote"
LIVE = ("service", "web", "runtime", "safety", "compiler", "agent", "recorder", "contracts")


def imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


class TestTheLiveRuntimeIsFreeOfTheEvaluator:
    @pytest.mark.parametrize("package", LIVE)
    def test_no_live_package_imports_the_evaluator(self, package: str) -> None:
        offenders = []
        for path in sorted((ROOT / package).rglob("*.py")):
            reached = {name for name in imports(path) if name.startswith("rote.eval")}
            if reached:
                offenders.append(f"{package}/{path.name} -> {sorted(reached)}")
        assert offenders == [], f"the evaluator leaked into the live runtime: {offenders}"

    def test_the_live_classifier_lives_in_the_runtime(self) -> None:
        from rote.runtime.classifier_rules import PRIORITY, StructuredFieldsClassifier

        assert PRIORITY
        assert StructuredFieldsClassifier().is_local is True

    def test_system_construction_lives_in_bootstrap(self) -> None:
        from rote.bootstrap.system import CompiledSystem, compile_and_activate

        assert callable(compile_and_activate)
        assert CompiledSystem.__name__ == "CompiledSystem"

    def test_the_router_owns_its_confidence_default(self) -> None:
        from rote.runtime.router import DEFAULT_MIN_CONFIDENCE_PER_MILLE

        assert 0 < DEFAULT_MIN_CONFIDENCE_PER_MILLE <= 1000

    # the evaluator may use the shared bootstrap, never the other way round
    def test_bootstrap_does_not_import_the_evaluator_or_the_demo(self) -> None:
        for path in sorted((ROOT / "bootstrap").rglob("*.py")):
            reached = {
                name
                for name in imports(path)
                if name.startswith(("rote.eval", "rote.service", "rote.web"))
            }
            assert reached == set(), f"bootstrap reaches {sorted(reached)}"

    def test_the_evaluation_double_still_resolves_to_the_same_class(self) -> None:
        import rote.eval.classifier_double as evaluated
        import rote.runtime.classifier_rules as live

        assert evaluated.StructuredFieldsClassifier is live.StructuredFieldsClassifier
