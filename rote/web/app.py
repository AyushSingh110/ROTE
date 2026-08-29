from __future__ import annotations

import asyncio
import collections
import csv
import dataclasses
import io
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from rote.bootstrap.evidence_corruption import EvidenceError
from rote.observability.logging import get_logger
from rote.service.scenario import (
    SCENARIOS,
    Decision,
    ScenarioId,
    ScenarioResult,
    run_scenario,
)
from rote.service.session import (
    LLM_MODE,
    ROTE_CLASSIFIER,
    ResolutionView,
    SessionRuntime,
    live_session,
    reset_session,
)

HERE = Path(__file__).parent
BANNER = "Synthetic benchmark — no real payment rail"
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
# candidate default, off unless explicitly switched on: the frozen V2 path stays reproducible
VERIFY_ENV = "ROTE_VERIFY_EVIDENCE"


def verification_enabled() -> bool:
    return os.environ.get(VERIFY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# an unreadable or unbuildable selection raises here, at startup, rather than turning into a
# quiet fall back to the deterministic classifier that nobody would notice
def llm_enabled() -> bool:
    return os.environ.get(ROTE_CLASSIFIER, "").strip().lower() == LLM_MODE


# Paths that answer while the plans are still compiling. Everything else is held: a route
# that quietly built the runtime on demand would compile it a second time, and would let a
# request act before the system is genuinely able to.
ALWAYS_REACHABLE = frozenset({"/health"})
WARMING_SECONDS_HINT = 180
WARMING_PAGE = (HERE / "templates" / "warming.html").read_text(encoding="utf-8")


@dataclasses.dataclass
class Readiness:
    """Whether the runtime can actually serve. Never set optimistically."""

    ready: bool = False
    seconds: float | None = None
    error: str | None = None

    def finished(self, seconds: float) -> None:
        self.seconds = round(seconds, 2)
        self.ready = True

    def failed(self, error: BaseException) -> None:
        # a misconfigured deployment stays not-ready and says why, rather than serving wrongly
        self.error = f"{type(error).__name__}: {error}"[:200]
        self.ready = False


class ResolveRequest(BaseModel):
    exception_id: str = Field(min_length=1)


# the three demonstration controls, deliberately a closed set: no arbitrary mutation is exposed
DEMO_CORRUPTIONS: tuple[tuple[EvidenceError, str], ...] = (
    (EvidenceError.AMOUNT_OFF_BY_ONE, "Bank amount off by one minor unit"),
    (EvidenceError.REFERENCE_SUBSTITUTION, "Bank reference replaced"),
    (EvidenceError.CROSS_CATEGORY, "Evidence rewritten to fit another procedure"),
)


def _corruption(name: str) -> EvidenceError:
    for error, _caption in DEMO_CORRUPTIONS:
        if error.value == name:
            return error
    raise HTTPException(status_code=404, detail=f"no demo corruption {name!r}")


def warmup() -> float:
    started = time.perf_counter()
    for scenario in ScenarioId:
        run_scenario(scenario)
    live_session(verification_enabled(), llm_enabled())
    return time.perf_counter() - started


# compiling is CPU-bound and takes minutes on a small host, so it runs off the event loop
# while the port already accepts connections. Requests are held until it finishes.
async def _warm_in_background(readiness: Readiness) -> None:
    _logger.info("warmup_started", note="compiling plans; requests are held until this finishes")
    started = time.perf_counter()
    try:
        await asyncio.to_thread(warmup)
    except Exception as error:
        readiness.failed(error)
        _logger.info("warmup_failed", error=type(error).__name__)
        return
    readiness.finished(time.perf_counter() - started)
    _logger.info("warmup_complete", seconds=readiness.seconds, scenarios=len(ScenarioId))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_warm_in_background(app.state.readiness))
    yield
    task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="Rote", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.readiness = Readiness()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def hold_until_ready(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if request.app.state.readiness.ready or path in ALWAYS_REACHABLE:
            return await call_next(request)
        if path.startswith("/static"):
            return await call_next(request)
        if path.startswith("/api"):
            return JSONResponse(
                status_code=503,
                content={
                    "ready": False,
                    "warming_up": True,
                    "detail": "Rote is still compiling its procedures; nothing is served yet",
                },
            )
        return HTMLResponse(content=WARMING_PAGE, status_code=503)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "banner": BANNER,
                "screens": SCREENS,
                "classifier_label": "Real LLM" if llm_enabled() else "Deterministic",
                "classifier_model_id": session().classifier_model_id,
                **context,
            },
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
        return live_session(verification_enabled(), llm_enabled())

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
            rows=[(item, runtime.triage(item.exception_id)) for item in items],
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
            corruptions=DEMO_CORRUPTIONS,
        )

    @app.post("/live/{exception_id}/corrupt/{corruption}")
    def corrupt_case(exception_id: str, corruption: str) -> RedirectResponse:
        _known(exception_id)
        session().corrupt_case(exception_id, _corruption(corruption))
        return RedirectResponse(url=f"/live/{exception_id}", status_code=303)

    @app.post("/live/{exception_id}/restore")
    def restore_case(exception_id: str) -> RedirectResponse:
        _known(exception_id)
        session().restore_case(exception_id)
        return RedirectResponse(url=f"/live/{exception_id}", status_code=303)

    @app.post("/live/{exception_id}/resolve")
    def resolve_case(exception_id: str) -> RedirectResponse:
        _known(exception_id)
        session().resolve(exception_id)
        return RedirectResponse(url=f"/live/{exception_id}", status_code=303)

    @app.get("/exceptions", response_class=HTMLResponse)
    def exceptions_page(request: Request) -> HTMLResponse:
        runtime = session()
        rows = runtime.exception_report()
        backlog = runtime.backlog()
        counted = collections.Counter(row.reason for row in rows)
        return page(
            request,
            "exceptions.html",
            rows=rows,
            total=len(backlog),
            automated=sum(1 for item in backlog if item.status == "automated"),
            worked=sum(1 for row in rows if row.worked)
            + sum(1 for item in backlog if item.status == "automated"),
            reasons=sorted(counted.items(), key=lambda pair: (-pair[1], pair[0])),
        )

    # the exception list an operator would actually take away with them
    @app.get("/api/exceptions.csv", response_class=PlainTextResponse)
    def exceptions_csv() -> PlainTextResponse:
        rows = session().exception_report()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            [
                "exception_id",
                "status",
                "worked",
                "reason",
                "fitting_count",
                "fitting_categories",
                "internal_minor_units",
                "internal_currency",
                "bank_minor_units",
                "bank_currency",
                "captured_on",
                "plan_lookups",
                "compiled_steps_executed",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.exception_id,
                    row.status,
                    "yes" if row.worked else "no",
                    row.reason,
                    row.fitting_count,
                    " | ".join(row.fitting_categories),
                    row.internal_minor_units,
                    row.internal_currency,
                    "" if row.bank_minor_units is None else row.bank_minor_units,
                    row.bank_currency or "",
                    row.captured_on,
                    row.plan_lookups,
                    row.compiled_steps_executed,
                ]
            )
        return PlainTextResponse(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"content-disposition": 'attachment; filename="rote-exceptions.csv"'},
        )

    @app.get("/api/exceptions")
    def exceptions_json() -> dict[str, Any]:
        rows = session().exception_report()
        return {
            "unresolved": len(rows),
            "reasons": dict(collections.Counter(row.reason for row in rows)),
            "items": [row.model_dump() for row in rows],
        }

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger_page(request: Request) -> HTMLResponse:
        runtime = session()
        return page(
            request, "ledger.html", ledger=runtime.ledger_view(), world=runtime.world_view()
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        readiness: Readiness = app.state.readiness
        if not readiness.ready:
            # the runtime does not exist yet, so nothing here may touch it
            return {
                "ready": False,
                "warming_up": True,
                "warmup_seconds": readiness.seconds,
                "warmup_error": readiness.error,
                "expected_warmup_seconds_hint": WARMING_SECONDS_HINT,
                "research_grade": False,
                "verify_evidence": verification_enabled(),
                "classifier": LLM_MODE if llm_enabled() else "deterministic",
            }
        runtime = session()
        return {
            "ready": True,
            "warming_up": False,
            "warmup_error": readiness.error,
            "warmup_seconds": readiness.seconds,
            "scenarios": len(ScenarioId),
            "backlog": len(runtime.backlog()),
            "ledger_entries": len(runtime.ledger.entries),
            "ledger_valid": runtime.ledger_view().valid,
            "research_grade": False,
            "verify_evidence": runtime.verifies_evidence,
            "classifier": LLM_MODE if llm_enabled() else "deterministic",
            "classifier_model_id": runtime.classifier_model_id,
        }

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        runtime = reset_session(verification_enabled(), llm_enabled())
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

__all__ = [
    "ADVERSARIAL",
    "ALWAYS_REACHABLE",
    "BANNER",
    "BASELINES",
    "Readiness",
    "app",
    "create_app",
    "warmup",
]
