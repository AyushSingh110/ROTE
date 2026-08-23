import ast
import pathlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rote.service.scenario import ScenarioId
from rote.web.app import create_app

WEB = pathlib.Path(__file__).resolve().parents[2] / "rote" / "web"
LOWER_LAYERS = ("contracts", "safety", "domain", "recorder", "compiler", "runtime", "agent", "eval")


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as started:
        yield started


class TestTheHttpLayerIsThin:
    # the API must call the facade, not rebuild any of it
    def test_it_imports_no_lower_layer_directly(self) -> None:
        tree = ast.parse((WEB / "app.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        reached = {
            name
            for name in imported
            if name.startswith("rote.") and name.split(".")[1] in LOWER_LAYERS
        }
        assert reached == set(), f"http layer reaches into {reached}"

    def test_it_defines_no_route_or_guard_logic(self) -> None:
        source = (WEB / "app.py").read_text(encoding="utf-8")
        for banned in ("precondition", "execute_plan", "PolicyGate", "Guard("):
            assert banned not in source


class TestTheHtmlScreens:
    def test_the_index_loads(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "research grade: False" in response.text

    @pytest.mark.parametrize("scenario", [s.value for s in ScenarioId])
    @pytest.mark.parametrize("screen", ["investigation", "evidence", "decision"])
    def test_every_screen_of_every_scenario_renders(
        self, client: TestClient, scenario: str, screen: str
    ) -> None:
        response = client.get(f"/s/{scenario}/{screen}")
        assert response.status_code == 200
        assert "Traceback" not in response.text
        assert "research grade: False" in response.text

    def test_the_refusal_screen_states_the_reason_plainly(self, client: TestClient) -> None:
        page = client.get("/s/ambiguous/decision").text
        assert "REFUSE AUTOMATION" in page
        assert "fee_mismatch" in page and "partial_payment" in page

    def test_the_automation_screen_shows_the_replay(self, client: TestClient) -> None:
        page = client.get("/s/automated/decision").text
        assert "AUTOMATE" in page
        assert "byte for byte" in page

    def test_untrusted_text_is_labelled_on_the_investigation_screen(
        self, client: TestClient
    ) -> None:
        assert "untrusted" in client.get("/s/injected_note/investigation").text

    def test_an_unknown_scenario_is_a_404(self, client: TestClient) -> None:
        assert client.get("/s/nope/investigation").status_code == 404

    def test_an_unknown_screen_is_a_404(self, client: TestClient) -> None:
        assert client.get("/s/automated/nope").status_code == 404


class TestTheJsonApi:
    def test_the_scenario_list_is_json(self, client: TestClient) -> None:
        body = client.get("/api/scenarios").json()
        assert body["research_grade"] is False
        assert {s["id"] for s in body["scenarios"]} == {s.value for s in ScenarioId}

    def test_a_scenario_serialises_completely(self, client: TestClient) -> None:
        body = client.get("/api/scenarios/automated").json()
        assert body["research_grade"] is False
        assert body["investigation"]["exception_id"]
        assert body["evidence"]["fitting_categories"] == ["fx_rounding"]
        assert body["decision"]["decision"] == "automate"

    def test_an_unknown_scenario_is_a_404(self, client: TestClient) -> None:
        response = client.get("/api/scenarios/not_a_scenario")
        assert response.status_code == 404
        assert "not_a_scenario" in response.json()["detail"]

    def test_repeated_requests_return_an_identical_decision(self, client: TestClient) -> None:
        first = client.get("/api/scenarios/ambiguous").json()
        second = client.get("/api/scenarios/ambiguous").json()
        assert first == second

    # the safety property has to survive the HTTP boundary, not just the Python call
    def test_a_refusal_over_http_shows_no_lookup_and_no_execution(self, client: TestClient) -> None:
        body = client.get("/api/scenarios/ambiguous").json()
        assert body["evidence"]["plan_lookups"] == 0
        assert body["decision"]["compiled_steps_executed"] == 0
        assert body["decision"]["plan_id"] is None
        assert body["decision"]["ledger_entries"] == 0
        assert body["decision"]["world_hash_before"] == body["decision"]["world_hash_after"]

    def test_no_scenario_leaks_state_into_another(self, client: TestClient) -> None:
        before = client.get("/api/scenarios/automated").json()
        for scenario in ScenarioId:
            client.get(f"/api/scenarios/{scenario.value}")
        assert client.get("/api/scenarios/automated").json() == before
