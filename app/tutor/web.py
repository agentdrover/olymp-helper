"""Маршруты разбора задания (ребёнок). JSON по умолчанию; с заголовком HX-Request - HTML-фрагмент
`child/_tutor.html`. Любой ответ строится из `build_view`."""

import sqlite3
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.chat.flow import ChatFlow
from app.ingest.publish import is_published
from app.ingest.store import atomic, get_task, open_db
from app.ingest.web import child_notice_gate
from app.notice import notice_redirect, notice_seen
from app.roles import PARENT, current_role
from app.tutor import seq, state, steps
from app.tutor.flow import Flow
from app.tutor.panel import BUSY
from app.tutor.state import Refused
from app.tutor.today import build_today
from app.tutor.view import build_view, snapshot_live
from app.voice.service import VOICE_STATES

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
TEMPLATES.env.globals["VOICE_STATES"] = VOICE_STATES
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
    message: str | None = None


def _published(conn, task_id: int):
    task = get_task(conn, task_id)
    if task is None or not is_published(task):
        raise HTTPException(404, "Задание не найдено")
    material = conn.execute(
        "SELECT year, original_name FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    return task, material


def _needs_notice(request: Request, conn) -> bool:
    """Ребёнок видит задания только после уведомления о внешней модели (SPEC 1.6); родителю оно
    не нужно. HTML-маршруты отправляют на уведомление, JSON и POST отвечают 403."""
    return current_role(request) != PARENT and not notice_seen(conn)


def child_same_origin(request: Request) -> None:
    """Изменяющий запрос ребёнка идёт со своей страницы и несёт JSON.

    У ребёнка нет сессии и токена, поэтому защита - источник запроса: (а) Sec-Fetch-Site, если он
    есть, только same-origin или none; (б) Origin, если он есть, совпадает с адресом, по которому
    открыт сайт, по host:port (схему не сравниваем: за TLS-прокси uvicorn видит http, а браузер -
    https); (в) Content-Type только application/json: обычная HTML-форма его отправить не может,
    а запрос с чужой страницы с таким типом требует предварительного запроса CORS, который мы не
    разрешаем. Клиенты без Origin и Sec-Fetch-Site (тесты, старые браузеры) проходят по (в).
    """
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    foreign = site is not None and site not in ("same-origin", "none")
    if not foreign and origin is not None:
        foreign = urlsplit(origin).netloc != request.url.netloc
    if foreign:
        raise HTTPException(
            403, detail={"error": "cross_origin", "message": "Чужой источник запроса."}
        )
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise HTTPException(
            415, detail={"error": "json_required", "message": "Запрос должен быть JSON."}
        )


def _is_fragment(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _step_result(conn, task_id: int, step_id: int | None):
    """(состояние панели, пояснение) окончившегося шага; (None, None), пока он идёт."""
    row = steps.get(conn, step_id) if step_id else None
    if row is None or row["task_item_id"] != task_id or row["status"] == "running":
        return None, None
    newest = steps.latest(conn, task_id)
    if newest is None or newest["id"] != row["id"]:  # итог устаревшего номера не показываем
        return None, None
    if row["status"] == "failed":
        return "step_failed", None
    return row["panel_state"], row["notice"]


def _view_of(
    request: Request, conn, task_id: int, *, step: int | None = None, flow=None, notice=None
):
    """Один словарь экрана для POST и GET. `step` - шаг, чей итог ждёт экран: когда он окончен,
    его итог (отказ модели, пояснение) показывается один раз."""
    app = request.app
    _published(conn, task_id)  # 404 до всего остального
    version = seq.current(conn)  # ПЕРВОЙ: данные новее версии безвредны, версия новее данных - нет
    live = snapshot_live(conn, app.state.inflight, task_id, app.state.clock())  # сначала: идёт ли
    with seq.read_snapshot(conn):  # данные экрана - из одного состояния базы
        return _view_in_snapshot(request, conn, task_id, live, step, flow, notice, version)


def _view_in_snapshot(request: Request, conn, task_id: int, live, step, flow, notice, version):
    app = request.app
    task, material = _published(conn, task_id)
    # Итог шага - ДО прогона: если шаг найден оконченным, прогон читается после его записи, и ответ
    # с итогом (например «помощник не ответил») не прячет подсказку, которую шаг уже записал.
    # Снимок «идёт шаг» главнее итога: пока какой-то шаг идёт, ответ занят ИМЕННО им, а итог
    # более старого шага не показывается (иначе вкладка бросит опрос и пропустит новый шаг).
    shown, note = (None, None) if live in BUSY else _step_result(conn, task_id, step)
    run = state.active_run(conn, task)
    if flow is not None:
        shown = shown or flow.panel_state
        note = note or flow.notice
    return build_view(
        conn, task, material, run, app.state.clock(),
        inflight=app.state.inflight, panel_state=shown, notice=notice or note, live=live,
        version=version,
    )  # fmt: skip


def _fragment(request: Request, view: dict):
    return TEMPLATES.TemplateResponse(
        request, "child/_tutor.html", {"v": view, "task": view["task"]}
    )


def _respond(request: Request, conn, task_id: int, flow: Flow | None = None, notice=None):
    view = _view_of(
        request, conn, task_id, step=flow.step_id if flow else None, flow=flow, notice=notice
    )
    started = flow is not None and flow.launched
    headers = {"X-Step-Started": "1"} if started else {}
    if flow is not None and flow.attempt_state:
        headers["X-Attempt"] = flow.attempt_state  # принята ли попытка: клиент сбрасывает форму
    headers = headers or None
    if _is_fragment(request):
        resp = _fragment(request, view)
        if headers:
            resp.headers.update(headers)
        return resp
    return JSONResponse(view, headers=headers)


def _refused(request: Request, conn, task_id: int, exc: Refused):
    if _is_fragment(request):
        return _respond(request, conn, task_id, notice=exc.message)
    return JSONResponse(
        {"ok": False, "code": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status
    )


def _flow(request: Request, conn, cls=Flow) -> Flow:
    app = request.app
    return cls(conn, app.state.model, app.state.inflight, app.state.clock, app.state.steps)


def _act(name: str, cls=Flow):
    def decorate(fn):
        @router.post(
            "/tasks/{task_id}/" + name,
            dependencies=[Depends(child_same_origin), Depends(child_notice_gate)],
        )
        def route(task_id: int, request: Request, conn: Db, body: Body | None = None):
            _published(conn, task_id)
            flow = _flow(request, conn, cls)
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


@_act("chat", ChatFlow)
def chat(flow: ChatFlow, task_id, body, request):
    flow.chat(task_id, body.message, body.mode)


@_act("show-answer")
def show_answer(flow: Flow, task_id, body, request):
    flow.show_answer(task_id, body.confirm)


@_act("repeat")
def repeat(flow: Flow, task_id, body, request):
    flow.repeat(task_id)


@_act("dispute")
def dispute(flow: Flow, task_id, body, request):
    flow.dispute(task_id)


@router.get("/api/tasks/{task_id}/tutor", dependencies=[Depends(child_notice_gate)])
def tutor_json(task_id: int, request: Request, conn: Db, step: int | None = None):
    return JSONResponse(_view_of(request, conn, task_id, step=step))


@router.get("/tasks/{task_id}/tutor", response_class=HTMLResponse)
def tutor_fragment(task_id: int, request: Request, conn: Db, step: int | None = None):
    _published(conn, task_id)
    if _needs_notice(request, conn):
        return notice_redirect(f"/tasks/{task_id}")
    return _fragment(request, _view_of(request, conn, task_id, step=step))


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(task_id: int, request: Request, conn: Db):
    task, material = _published(conn, task_id)
    if _needs_notice(request, conn):
        return notice_redirect(f"/tasks/{task_id}")
    app = request.app
    version = seq.current(conn)  # первой, до замка и данных
    live = snapshot_live(conn, app.state.inflight, task_id, app.state.clock())  # до чтения прогона
    with atomic(conn):  # экран открыт: с этого момента идут 60 секунд до подсказки 1
        state.ensure_run(conn, task_id, app.state.clock())
    with seq.read_snapshot(conn):
        task, material = _published(conn, task_id)
        view = build_view(
            conn, task, material, state.active_run(conn, task), app.state.clock(),
            inflight=app.state.inflight, live=live, version=version,
        )  # fmt: skip
    return TEMPLATES.TemplateResponse(request, "child/task.html", {"v": view, "task": view["task"]})


@router.get("/today", response_class=HTMLResponse)
def today_page(request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect("/today")
    data = build_today(conn, request.app.state.clock())
    return TEMPLATES.TemplateResponse(request, "child/today.html", {"t": data})
