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


def _first(client: TestClient, **match: object) -> str:
    for item in client.get("/api/backlog").json()["items"]:
        page = client.get(f"/live/{item['exception_id']}").text
        if all(str(value) in page for value in match.values()):
            return str(item["exception_id"])
    raise LookupError(f"no live case matching {match}")


class TestTheLiveQueue:
    def test_the_queue_page_renders(self, client: TestClient) -> None:
        response = client.get("/queue")
        assert response.status_code == 200
        assert "Traceback" not in response.text
        assert "EXC-" in response.text

    def test_the_backlog_is_json_and_hides_ground_truth(self, client: TestClient) -> None:
        body = client.get("/api/backlog").json()
        assert body["total"] > 0
        assert body["items"]
        assert "category" not in body["items"][0]

    def test_a_live_case_page_renders(self, client: TestClient) -> None:
        first = client.get("/api/backlog").json()["items"][0]["exception_id"]
        response = client.get(f"/live/{first}")
        assert response.status_code == 200
        assert "untrusted" in response.text

    def test_an_unknown_exception_is_a_404(self, client: TestClient) -> None:
        assert client.get("/live/EXC-nope").status_code == 404
        assert client.post("/api/resolve", json={"exception_id": "EXC-nope"}).status_code == 404

    def test_a_malformed_resolve_body_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/resolve", json={}).status_code == 422


class TestResolvingOverHttp:
    def test_an_ambiguous_case_refuses_without_touching_a_plan(self, client: TestClient) -> None:
        target = _first(client, reason="ambiguous_evidence")
        before = client.get("/api/world").json()["world_hash"]
        ledger_before = client.get("/api/ledger").json()["total"]

        body = client.post("/api/resolve", json={"exception_id": target}).json()
        assert body["decision"] == "escalate"
        assert body["route_reason"] == "ambiguous_evidence"
        assert body["plan_lookups"] == 0
        assert body["compiled_steps_executed"] == 0
        assert body["plan_id"] is None
        assert len(body["co_holding_categories"]) > 1
        assert body["world_changed"] is False

        assert client.get("/api/world").json()["world_hash"] == before
        assert client.get("/api/ledger").json()["total"] == ledger_before

    def test_an_unambiguous_case_automates_and_moves_the_world(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        before = client.get("/api/world").json()["world_hash"]
        body = client.post("/api/resolve", json={"exception_id": target}).json()
        assert body["decision"] == "automate"
        assert body["plan_lookups"] == 1
        assert body["compiled_steps_executed"] > 0
        assert body["guard_inspections"] >= body["compiled_steps_executed"]
        assert body["model_calls_after_classification"] == 0
        assert body["outcome_hash"]
        assert client.get("/api/world").json()["world_hash"] != before

    def test_resolving_the_same_case_twice_acts_once(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        client.post("/api/resolve", json={"exception_id": target})
        after_first = client.get("/api/world").json()["world_hash"]
        ledger_after_first = client.get("/api/ledger").json()["total"]

        second = client.post("/api/resolve", json={"exception_id": target}).json()
        assert second["already_resolved"] is True
        assert second["world_changed"] is False
        assert client.get("/api/world").json()["world_hash"] == after_first
        assert client.get("/api/ledger").json()["total"] == ledger_after_first

    def test_the_ledger_stays_valid_after_resolving(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        client.post("/api/resolve", json={"exception_id": target})
        ledger = client.get("/api/ledger").json()
        assert ledger["valid"] is True
        assert ledger["first_broken_seq"] is None

    def test_the_form_post_redirects_back_to_the_case(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        response = client.post(f"/live/{target}/resolve", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/live/{target}"

    def test_the_ledger_page_renders(self, client: TestClient) -> None:
        response = client.get("/ledger")
        assert response.status_code == 200
        assert "Traceback" not in response.text
        assert "valid" in response.text


class TestPresentationAffordances:
    def test_health_reports_readiness(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["ready"] is True
        assert body["research_grade"] is False
        assert body["backlog"] > 0
        assert body["ledger_valid"] is True

    def test_the_landing_page_answers_the_product_questions(self, client: TestClient) -> None:
        page = client.get("/").text
        for phrase in (
            "direct authority to move money",
            "authority layer between AI reasoning and financial actions",
            "Does exactly one procedure fit the evidence",
            "AUTOMATE",
            "REFUSE AUTOMATION",
            "research grade: False",
        ):
            assert phrase in page, f"landing page is missing: {phrase}"

    def test_the_landing_page_carries_the_v1_v2_result(self, client: TestClient) -> None:
        page = client.get("/").text
        for number in ("500", "184", "36.8%", "88.0%", "100%", "60"):
            assert number in page
        assert "deliberate, not a regression" in page

    def test_the_three_scenarios_are_offered(self, client: TestClient) -> None:
        page = client.get("/").text
        assert "Scenario A" in page and "Scenario B" in page and "Scenario C" in page
        for target in ("/s/automated/", "/s/ambiguous/", "/s/schema_drift/"):
            assert target in page

    def test_demo_mode_is_labelled_on_every_page(self, client: TestClient) -> None:
        for path in ("/", "/queue", "/ledger", "/s/ambiguous/decision"):
            assert "DEMO MODE" in client.get(path).text

    def test_reset_restores_a_clean_session(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        client.post("/api/resolve", json={"exception_id": target})
        assert client.get("/api/ledger").json()["total"] > 0

        body = client.post("/api/reset").json()
        assert body["reset"] is True
        assert body["ledger_entries"] == 0
        assert client.get("/api/ledger").json()["total"] == 0
        assert client.get("/api/world").json()["adjustments"] == 0
        assert client.get("/api/ledger").json()["valid"] is True

    def test_reset_leaves_the_backlog_intact(self, client: TestClient) -> None:
        before = client.get("/api/backlog").json()["total"]
        client.post("/api/reset")
        assert client.get("/api/backlog").json()["total"] == before

    def test_a_case_can_be_resolved_again_after_reset(self, client: TestClient) -> None:
        target = _first(client, reason="plan_matched")
        client.post("/api/resolve", json={"exception_id": target})
        client.post("/api/reset")
        again = client.post("/api/resolve", json={"exception_id": target}).json()
        assert again["already_resolved"] is False
        assert again["decision"] == "automate"
        assert again["world_changed"] is True


class TestTheVerificationCandidateDefault:
    def test_the_live_session_defaults_to_verification_off(self) -> None:
        from rote.service.session import live_session

        assert live_session().verifies_evidence is False

    def test_it_can_be_switched_on_through_the_live_path(self) -> None:
        from rote.service.session import live_session

        verified = live_session(verify_evidence=True)
        assert verified.verifies_evidence is True
        assert verified is not live_session()

    def test_the_environment_switch_selects_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rote.web.app import verification_enabled

        monkeypatch.delenv("ROTE_VERIFY_EVIDENCE", raising=False)
        assert verification_enabled() is False
        monkeypatch.setenv("ROTE_VERIFY_EVIDENCE", "1")
        assert verification_enabled() is True
        monkeypatch.setenv("ROTE_VERIFY_EVIDENCE", "0")
        assert verification_enabled() is False

    def test_health_reports_whether_verification_is_on(self, client: TestClient) -> None:
        assert client.get("/health").json()["verify_evidence"] is False

    def test_reset_preserves_the_verification_setting(self) -> None:
        from rote.service.session import live_session, reset_session

        verified = live_session(verify_evidence=True)
        assert verified.verifies_evidence is True
        again = reset_session(verify_evidence=True)
        assert again.verifies_evidence is True
        assert len(again.ledger.entries) == 0
