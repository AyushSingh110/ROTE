"""The exception report over HTTP, and the refusal cost pinned against frozen ground truth."""

from __future__ import annotations

import csv
import io
import json
import pathlib
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rote.web.app import create_app

BASELINES = pathlib.Path(__file__).resolve().parents[2] / "docs" / "baselines"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as started:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not started.get("/health").json()["ready"]:
            time.sleep(0.5)
        yield started


class TestTheExceptionPage:
    def test_it_loads_and_names_the_reasons(self, client: TestClient) -> None:
        response = client.get("/exceptions")
        assert response.status_code == 200
        assert "Traceback" not in response.text
        assert "ambiguous_evidence" in response.text
        assert "Synthetic benchmark" in response.text

    def test_it_reports_the_match_rate(self, client: TestClient) -> None:
        assert "match rate" in client.get("/exceptions").text.lower()

    def test_it_lists_actual_cases_not_only_counts(self, client: TestClient) -> None:
        page = client.get("/exceptions").text
        assert "EXC-" in page, "the report must name the cases themselves"


class TestTheCsvExport:
    def test_it_downloads_as_csv(self, client: TestClient) -> None:
        response = client.get("/api/exceptions.csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers.get("content-disposition", "")

    def test_every_row_carries_a_reason_and_the_money(self, client: TestClient) -> None:
        rows = list(csv.DictReader(io.StringIO(client.get("/api/exceptions.csv").text)))
        assert len(rows) > 100
        for row in rows[:50]:
            assert row["exception_id"].startswith("EXC-")
            assert row["reason"]
            assert int(row["internal_minor_units"]) > 0

    # the claim the whole product rests on, checked in the exported artifact
    def test_no_exported_exception_ever_reached_a_plan_or_a_step(self, client: TestClient) -> None:
        rows = list(csv.DictReader(io.StringIO(client.get("/api/exceptions.csv").text)))
        worked = [r for r in rows if r["worked"] == "yes"]
        for row in worked:
            assert int(row["plan_lookups"]) == 0, row["exception_id"]
            assert int(row["compiled_steps_executed"]) == 0, row["exception_id"]

    def test_the_json_form_agrees_with_the_csv(self, client: TestClient) -> None:
        body = client.get("/api/exceptions").json()
        rows = list(csv.DictReader(io.StringIO(client.get("/api/exceptions.csv").text)))
        assert body["unresolved"] == len(rows)
        assert sum(body["reasons"].values()) == len(rows)


# ---------------------------------------------------------------- item 3
def _verdicts(name: str) -> dict[str, dict[str, str]]:
    path = BASELINES / name / "runs.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return {r["task_id"]: r for r in rows if r.get("path") == "rote"}


class TestTheRefusalCostIsRealAndPinned:
    """What refusing actually costs, computed only from the immutable baselines."""

    def test_the_frozen_baselines_still_say_what_we_quote(self) -> None:
        v1, v2 = _verdicts("phase16_v1"), _verdicts("phase16_v2")
        assert len(v1) == 500 and len(v2) == 500
        assert sum(1 for r in v1.values() if r["checker_verdict"] != "pass") == 60
        assert sum(1 for r in v2.values() if r["route_reason"] == "plan_matched") == 184

    def test_every_v1_error_is_inside_the_refused_set(self) -> None:
        v1, v2 = _verdicts("phase16_v1"), _verdicts("phase16_v2")
        wrong = {t for t, r in v1.items() if r["checker_verdict"] != "pass"}
        refused = {t for t, r in v2.items() if r["route_reason"] != "plan_matched"}
        assert wrong <= refused, "a v1 error survived into the automated set"
        assert len(wrong) == 60

    def test_the_refusal_cost_ratio(self) -> None:
        v1, v2 = _verdicts("phase16_v1"), _verdicts("phase16_v2")
        refused = {t for t, r in v2.items() if r["route_reason"] != "plan_matched"}
        wrong = {t for t, r in v1.items() if r["checker_verdict"] != "pass"}
        saved = len(wrong & refused)
        given_up = len(refused) - saved
        assert saved == 60
        assert given_up == 256
        assert round(given_up / saved, 1) == 4.3
