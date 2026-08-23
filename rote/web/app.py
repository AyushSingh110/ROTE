from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from rote.observability.logging import get_logger
from rote.service.scenario import (
    SCENARIOS,
    Decision,
    ScenarioId,
    ScenarioResult,
    run_scenario,
)
from rote.service.session import (
    ResolutionView,
    SessionRuntime,
    live_session,
    reset_session,
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


QUEUE_PAGE = 60
_READY: dict[str, float] = {}


class ResolveRequest(BaseModel):
    exception_id: str = Field(min_length=1)


def warmup() -> float:
    started = time.perf_counter()
    for scenario in ScenarioId:
        run_scenario(scenario)
    live_session()
    return time.perf_counter() - started


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # compile once at startup so no presenter waits for it mid-demonstration. The port does not
    # accept connections until this finishes, so "it answers" is the readiness signal.
    _logger.info("warmup_started", note="compiling plans; the server is not serving yet")
    elapsed = warmup()
    _READY["seconds"] = round(elapsed, 2)
    _logger.info(
        "warmup_complete",
        seconds=_READY["seconds"],
        scenarios=len(ScenarioId),
        note="READY - open http://127.0.0.1:8000/",
    )
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

    def session() -> SessionRuntime:
        return live_session()

    def _known(exception_id: str) -> None:
        if exception_id not in {item.exception_id for item in session().backlog()}:
            raise HTTPException(status_code=404, detail=f"no exception {exception_id!r}")

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request) -> HTMLResponse:
        runtime = session()
        items = runtime.backlog()[:QUEUE_PAGE]
        return page(
            request,
            "queue.html",
            rows=[(item, runtime.preview(item.exception_id)) for item in items],
            shown=len(items),
            total=len(runtime.backlog()),
        )

    @app.get("/live/{exception_id}", response_class=HTMLResponse)
    def live_case(request: Request, exception_id: str) -> HTMLResponse:
        _known(exception_id)
        runtime = session()
        return page(
            request,
            "live.html",
            detail=runtime.investigation(exception_id),
            preview=runtime.preview(exception_id),
            resolution=runtime.resolution_for(exception_id),
            world=runtime.world_view(),
        )

    @app.post("/live/{exception_id}/resolve")
    def resolve_case(exception_id: str) -> RedirectResponse:
        _known(exception_id)
        session().resolve(exception_id)
        return RedirectResponse(url=f"/live/{exception_id}", status_code=303)

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger_page(request: Request) -> HTMLResponse:
        runtime = session()
        return page(
            request, "ledger.html", ledger=runtime.ledger_view(), world=runtime.world_view()
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        runtime = session()
        return {
            "ready": True,
            "warmup_seconds": _READY.get("seconds"),
            "scenarios": len(ScenarioId),
            "backlog": len(runtime.backlog()),
            "ledger_entries": len(runtime.ledger.entries),
            "ledger_valid": runtime.ledger_view().valid,
            "research_grade": False,
        }

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        runtime = reset_session()
        return {
            "reset": True,
            "backlog": len(runtime.backlog()),
            "ledger_entries": len(runtime.ledger.entries),
            "world_hash": runtime.world_view().world_hash,
        }

    @app.get("/api/backlog")
    def api_backlog() -> dict[str, Any]:
        runtime = session()
        return {
            "total": len(runtime.backlog()),
            "items": [item.model_dump(mode="json") for item in runtime.backlog()[:QUEUE_PAGE]],
        }

    @app.post("/api/resolve")
    def api_resolve(body: ResolveRequest) -> dict[str, Any]:
        _known(body.exception_id)
        resolution: ResolutionView = session().resolve(body.exception_id)
        return resolution.model_dump(mode="json")

    @app.get("/api/ledger")
    def api_ledger() -> dict[str, Any]:
        return session().ledger_view().model_dump(mode="json")

    @app.get("/api/world")
    def api_world() -> dict[str, Any]:
        return session().world_view().model_dump(mode="json")

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
