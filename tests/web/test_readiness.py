"""The port opens before the plans are compiled, and readiness is reported honestly.

A container that refuses connections for minutes looks broken. A container that pretends to be
ready while the runtime is still compiling would be worse, so nothing here fakes readiness: the
app answers, says it is warming up, and refuses to do any work until it genuinely can.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from rote.web.app import ALWAYS_REACHABLE, create_app


@pytest.fixture
def warming() -> TestClient:
    """An app whose lifespan has not run, so the warmup has not started."""
    app = create_app()
    assert app.state.readiness.ready is False
    return TestClient(app)


@pytest.fixture(scope="module")
def warm() -> TestClient:
    with TestClient(create_app()) as started:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not started.get("/health").json()["ready"]:
            time.sleep(0.5)
        yield started


class TestBeforeWarmupFinishes:
    def test_health_answers_and_says_it_is_not_ready(self, warming: TestClient) -> None:
        response = warming.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["warming_up"] is True
        assert body["research_grade"] is False

    def test_health_still_reports_the_configured_provider(self, warming: TestClient) -> None:
        body = warming.get("/health").json()
        assert "classifier" in body
        assert "verify_evidence" in body

    def test_a_page_shows_a_warming_state_rather_than_a_traceback(
        self, warming: TestClient
    ) -> None:
        response = warming.get("/queue")
        assert response.status_code == 503
        assert "warming up" in response.text.lower()
        assert "Traceback" not in response.text
        assert "Internal Server Error" not in response.text

    @pytest.mark.parametrize("path", ["/", "/queue", "/ledger", "/api/backlog", "/api/ledger"])
    def test_every_working_route_is_held(self, warming: TestClient, path: str) -> None:
        assert warming.get(path).status_code == 503

    def test_health_is_always_reachable(self, warming: TestClient) -> None:
        assert "/health" in ALWAYS_REACHABLE

    # the whole point of holding requests: nothing may reach a tool before the system is ready
    def test_no_request_can_move_money_before_readiness(self, warming: TestClient) -> None:
        resolved = warming.post("/api/resolve", json={"exception_id": "EXC-000004"})
        assert resolved.status_code == 503
        assert warming.post("/api/reset").status_code == 503
        assert warming.post("/live/EXC-000004/resolve").status_code == 503
        assert warming.post("/live/EXC-000004/corrupt/cross_category").status_code == 503

    def test_holding_a_request_leaves_no_ledger_behind(self, warming: TestClient) -> None:
        warming.post("/api/resolve", json={"exception_id": "EXC-000004"})
        body = warming.get("/health").json()
        # the runtime was never built, so there is nothing it could have written
        assert body.get("ledger_entries") in (None, 0)


class TestAfterWarmupFinishes:
    def test_it_reports_ready_with_a_measured_time(self, warm: TestClient) -> None:
        body = warm.get("/health").json()
        assert body["ready"] is True
        assert body["warming_up"] is False
        assert isinstance(body["warmup_seconds"], float)
        assert body["warmup_error"] is None

    def test_the_existing_health_fields_are_unchanged(self, warm: TestClient) -> None:
        body = warm.get("/health").json()
        for field in (
            "backlog",
            "scenarios",
            "ledger_entries",
            "ledger_valid",
            "research_grade",
            "verify_evidence",
            "classifier",
            "classifier_model_id",
        ):
            assert field in body, field
        assert body["backlog"] == 500
        assert body["ledger_valid"] is True

    @pytest.mark.parametrize("path", ["/", "/queue", "/ledger", "/api/backlog"])
    def test_the_application_behaves_normally_again(self, warm: TestClient, path: str) -> None:
        assert warm.get(path).status_code == 200

    def test_no_secret_appears_in_the_health_payload(self, warm: TestClient) -> None:
        raw = warm.get("/health").text.lower()
        for banned in ("api_key", "authorization", "bearer", "gsk_", "hf_", "token"):
            assert banned not in raw, banned
