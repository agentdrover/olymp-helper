"""Маршруты разбора задания (ребёнок). JSON по умолчанию; с заголовком HX-Request - HTML-фрагмент
`child/_tutor.html`. Любой ответ строится из `build_view`."""

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.ingest.publish import is_published
from app.ingest.store import atomic, get_task, open_db
from app.tutor import state
from app.tutor.flow import Flow
from app.tutor.state import Refused
from app.tutor.today import build_today
from app.tutor.view import build_view

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
router = APIRouter()


def db_of(request: Request):
    conn = open_db(request.app.state.ctx.db_path)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(db_of)]


class Body(BaseModel):
    answer: Any = None
    confidence: str | None = None
    level: int | None = None
    confirm: bool = False
    mode: str | None = None


def _published(conn, task_id: int):
    task = get_task(conn, task_id)
    if task is None or not is_published(task):
        raise HTTPException(404, "Задание не найдено")
    material = conn.execute(
        "SELECT year FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    return task, material


def _is_fragment(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _respond(request: Request, conn, task_id: int, flow: Flow | None = None, notice=None):
    app = request.app
    task, material = _published(conn, task_id)
    run = state.latest_run(conn, task_id)
    clock = app.state.clock
    view = build_view(
        conn, task, material, run, clock(),
        inflight=app.state.inflight,
        panel_state=flow.panel_state if flow else None,
        notice=notice or (flow.notice if flow else None),
    )  # fmt: skip
    if _is_fragment(request):
        return TEMPLATES.TemplateResponse(
            request, "child/_tutor.html", {"v": view, "task": view["task"]}
        )
    return JSONResponse(view)


def _refused(request: Request, conn, task_id: int, exc: Refused):
    if _is_fragment(request):
        return _respond(request, conn, task_id, notice=exc.message)
    return JSONResponse(
        {"ok": False, "code": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status
    )


def _flow(request: Request, conn) -> Flow:
    app = request.app
    return Flow(conn, app.state.model, app.state.inflight, app.state.clock)


def _act(name: str):
    def decorate(fn):
        @router.post("/tasks/{task_id}/" + name)
        def route(task_id: int, request: Request, conn: Db, body: Body | None = None):
            _published(conn, task_id)
            flow = _flow(request, conn)
            try:
                fn(flow, task_id, body or Body(), request)
            except Refused as exc:
                return _refused(request, conn, task_id, exc)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                return JSONResponse(
                    {"ok": False, "code": "busy", "message": "Занято, попробуй ещё раз."},
                    status_code=503, headers={"Retry-After": "2"},
                )  # fmt: skip
            return _respond(request, conn, task_id, flow)

        return fn

    return decorate


@_act("attempt")
def attempt(flow: Flow, task_id, body, request):
    flow.attempt(task_id, body.answer, body.confidence)


@_act("hint")
def hint(flow: Flow, task_id, body, request):
    # Ступень задаёт сервер; уровень из запроса только сверяется (перескочить нельзя).
    level = body.level
    if level is None and "level" in request.query_params:
        try:
            level = int(request.query_params["level"])
        except ValueError:
            raise Refused(422, "bad_level", "Подсказки бывают трёх ступеней: 1, 2 и 3.") from None
    flow.hint(task_id, level, body.confirm)


@_act("explain")
def explain(flow: Flow, task_id, body, request):
    flow.explain(task_id, body.mode)


@_act("show-answer")
def show_answer(flow: Flow, task_id, body, request):
    flow.show_answer(task_id, body.confirm)


@_act("repeat")
def repeat(flow: Flow, task_id, body, request):
    flow.repeat(task_id)


@_act("dispute")
def dispute(flow: Flow, task_id, body, request):
    flow.dispute(task_id)


@router.get("/api/tasks/{task_id}/tutor")
def tutor_json(task_id: int, request: Request, conn: Db):
    return _respond_json(request, conn, task_id)


def _respond_json(request, conn, task_id):
    task, material = _published(conn, task_id)
    app = request.app
    view = build_view(
        conn, task, material, state.latest_run(conn, task_id), app.state.clock(),
        inflight=app.state.inflight,
    )  # fmt: skip
    return JSONResponse(view)


@router.get("/tasks/{task_id}/tutor", response_class=HTMLResponse)
def tutor_fragment(task_id: int, request: Request, conn: Db):
    task, material = _published(conn, task_id)
    app = request.app
    view = build_view(
        conn, task, material, state.latest_run(conn, task_id), app.state.clock(),
        inflight=app.state.inflight,
    )  # fmt: skip
    return TEMPLATES.TemplateResponse(
        request, "child/_tutor.html", {"v": view, "task": view["task"]}
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(task_id: int, request: Request, conn: Db):
    task, material = _published(conn, task_id)
    app = request.app
    with atomic(conn):  # экран открыт: с этого момента идут 60 секунд до подсказки 1
        state.ensure_run(conn, task_id, app.state.clock())
    view = build_view(
        conn, task, material, state.latest_run(conn, task_id), app.state.clock(),
        inflight=app.state.inflight,
    )  # fmt: skip
    return TEMPLATES.TemplateResponse(request, "child/task.html", {"v": view, "task": view["task"]})


@router.get("/today", response_class=HTMLResponse)
def today_page(request: Request, conn: Db):
    data = build_today(conn, request.app.state.clock())
    return TEMPLATES.TemplateResponse(request, "child/today.html", {"t": data})
