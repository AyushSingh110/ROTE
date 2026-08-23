from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rote.observability.logging import get_logger
from rote.service.scenario import (
    SCENARIOS,
    Decision,
    ScenarioId,
    ScenarioResult,
    run_scenario,
)

HERE = Path(__file__).parent
BANNER = "Offline prototype — research grade: False"
SCREENS = ("investigation", "evidence", "decision")
ADVERSARIAL = (
    (ScenarioId.SCHEMA_DRIFT, "A bank response gains a field", "Guard"),
    (ScenarioId.CAP_BREACH, "An amount exceeds its per-action cap", "Policy gate"),
    (ScenarioId.KILL_SWITCH, "An active plan is withdrawn", "Plan registry"),
    (ScenarioId.INJECTED_NOTE, "A merchant note carries instructions", "Ingestion boundary"),
)
# the frozen baselines, quoted from docs/baselines/ and never recomputed here
BASELINES = {
    "v1": {"automated": 500, "refused": 0, "correct": 440, "wrong": 60, "coverage": "100%"},
    "v2": {"automated": 184, "refused": 316, "correct": 500, "wrong": 0, "coverage": "36.8%"},
}

_logger = get_logger("rote.web")


def _resolve(scenario_id: str) -> ScenarioId:
    try:
        return ScenarioId(scenario_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no scenario {scenario_id!r}") from None


def warmup() -> float:
    started = time.perf_counter()
    for scenario in ScenarioId:
        run_scenario(scenario)
    return time.perf_counter() - started


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # compile once at startup so no judge waits for it mid-presentation
    elapsed = warmup()
    _logger.info("warmup_complete", seconds=round(elapsed, 2), scenarios=len(ScenarioId))
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Rote", docs_url=None, redoc_url=None, lifespan=_lifespan)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"banner": BANNER, "screens": SCREENS, **context},
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return page(
            request,
            "index.html",
            specs=[SCENARIOS[scenario] for scenario in ScenarioId],
            adversarial=[
                (SCENARIOS[s], caption, defence, run_scenario(s))
                for s, caption, defence in ADVERSARIAL
            ],
            baselines=BASELINES,
            decision_names={"automate": Decision.AUTOMATE, "escalate": Decision.ESCALATE},
        )

    @app.get("/s/{scenario_id}/{screen}", response_class=HTMLResponse)
    def screen(request: Request, scenario_id: str, screen: str) -> HTMLResponse:
        if screen not in SCREENS:
            raise HTTPException(status_code=404, detail=f"no screen {screen!r}")
        scenario = _resolve(scenario_id)
        return page(
            request,
            f"{screen}.html",
            result=run_scenario(scenario),
            scenario_id=scenario.value,
            screen=screen,
        )

    @app.get("/api/scenarios")
    def list_scenarios() -> dict[str, Any]:
        return {
            "banner": BANNER,
            "research_grade": False,
            "scenarios": [SCENARIOS[scenario].model_dump(mode="json") for scenario in ScenarioId],
        }

    @app.get("/api/scenarios/{scenario_id}")
    def read_scenario(scenario_id: str) -> dict[str, Any]:
        result: ScenarioResult = run_scenario(_resolve(scenario_id))
        return result.model_dump(mode="json")

    return app


app = create_app()

__all__ = ["ADVERSARIAL", "BANNER", "BASELINES", "app", "create_app", "warmup"]
