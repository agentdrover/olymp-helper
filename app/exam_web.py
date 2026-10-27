"""Маршруты имитации олимпиады: формат (родитель), сборка, прохождение, сдача и итог (ребёнок)."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app import exam
from app.exam import ExamError
from app.ingest.web import TEMPLATES, Db, child_notice_gate
from app.notice import notice_redirect
from app.roles import csrf_token, now_of, require_csrf, require_parent
from app.tutor.web import _needs_notice, child_same_origin

child = APIRouter()
parent = APIRouter(dependencies=[Depends(require_parent), Depends(require_csrf)])
TEMPLATES.env.globals["csrf_token"] = csrf_token
JSON_ACTION = [Depends(child_same_origin), Depends(child_notice_gate)]


class Body(BaseModel):
    mode: str | None = None
    material_id: int | None = None
    section: str | None = None
    variant: str | None = None
    stage: str | None = None
    accept_reduced: bool = False
    position: int | None = None
    answer: Any = None
    marked: bool = False
    confirm: bool = False
    attempt_id: int | None = None


def _error(exc: ExamError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": exc.code, "message": exc.message, **exc.extra},
        status_code=exc.status,
    )


def _state(conn, now) -> dict:
    running = exam.active(conn, now)
    fmt = exam.get_format(conn)
    data = {
        "format": fmt,
        "message": None if fmt else exam.NOT_SET,
        "running_id": running["id"] if running else None,
        "history": exam.history(conn),
        "groups": [],
        "stock": {},
    }
    if fmt and not running:
        data["groups"] = exam.groups(conn)
        data["stock"] = exam.stock(conn, fmt)
    return data


# ----------------------------------------------------------------------- ребёнок


@child.get("/exam", response_class=HTMLResponse)
def exam_page(request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect("/exam")
    data = _state(conn, now_of(request))
    if data["running_id"] is not None:
        return RedirectResponse("/exam/run", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "child/exam.html", {"d": data, "type_labels": exam.TYPE_LABELS}
    )


@child.get("/api/exam", dependencies=[Depends(child_notice_gate)])
def exam_state(request: Request, conn: Db):
    return JSONResponse(_state(conn, now_of(request)))


@child.post("/exam/start", dependencies=JSON_ACTION)
def exam_start(request: Request, conn: Db, body: Body):
    now = now_of(request)
    try:
        attempt_id = exam.start(
            conn, now, mode=body.mode or "", material_id=body.material_id,
            section=body.section, variant=body.variant, stage=body.stage,
            accept_reduced=body.accept_reduced,
        )  # fmt: skip
    except ExamError as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "attempt_id": attempt_id, "url": "/exam/run"})


@child.get("/api/exam/current", dependencies=[Depends(child_notice_gate)])
def exam_current(request: Request, conn: Db):
    attempt = exam.active(conn, now_of(request))
    if attempt is None:
        return JSONResponse(
            {"ok": False, "code": "not_running", "message": "Вариант не идёт."}, status_code=404
        )
    return JSONResponse(exam.running_view(conn, attempt))


@child.get("/exam/run", response_class=HTMLResponse)
def exam_run(request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect("/exam/run")
    attempt = exam.active(conn, now_of(request))
    if attempt is None:
        return RedirectResponse("/exam", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "child/exam_run.html", {"type_labels": exam.TYPE_LABELS}
    )


def _act(path: str):
    def decorate(fn):
        @child.post(path, dependencies=JSON_ACTION)
        def route(request: Request, conn: Db, body: Body):
            now = now_of(request)
            try:
                fn(conn, now, body)
            except ExamError as exc:
                return _error(exc)
            attempt = exam.active(conn, now)
            view = exam.running_view(conn, attempt) if attempt else None
            return JSONResponse({"ok": True, "exam": view})

        return fn

    return decorate


def _position(body: Body) -> int:
    if body.position is None:
        raise ExamError(422, "no_position", "Не указан номер задания.")
    return body.position


@_act("/exam/open")
def _open(conn, now, body):
    exam.open_item(conn, now, _position(body))


@_act("/exam/answer")
def _answer(conn, now, body):
    exam.save_answer(conn, now, _position(body), body.answer)


@_act("/exam/mark")
def _mark(conn, now, body):
    exam.set_mark(conn, now, _position(body), body.marked)


@child.post("/exam/submit", dependencies=JSON_ACTION)
def exam_submit(request: Request, conn: Db, body: Body):
    now = now_of(request)
    attempt_id = body.attempt_id
    if attempt_id is None:
        running = exam.active(conn, now)
        if running is None:
            return _error(ExamError(409, "not_running", "Вариант не идёт."))
        attempt_id = running["id"]
    try:
        done = exam.submit(conn, now, attempt_id, confirm=body.confirm)
    except ExamError as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "url": f"/exam/results/{done['id']}", "result": done})


@child.get("/api/exam/results/{attempt_id}", dependencies=[Depends(child_notice_gate)])
def result_json(attempt_id: int, request: Request, conn: Db):
    exam.expire_due(conn, now_of(request))
    try:
        return JSONResponse(exam.result(conn, attempt_id))
    except ExamError as exc:
        return _error(exc)


@child.get("/exam/results/{attempt_id}", response_class=HTMLResponse)
def result_page(attempt_id: int, request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect(f"/exam/results/{attempt_id}")
    exam.expire_due(conn, now_of(request))
    try:
        data = exam.result(conn, attempt_id)
    except ExamError as exc:
        if exc.code == "exam_running":
            return RedirectResponse("/exam/run", status_code=303)
        return HTMLResponse(exc.message, status_code=exc.status)
    return TEMPLATES.TemplateResponse(request, "child/exam_result.html", {"r": data})


# ---------------------------------------------------------------------- родитель


def _format_page(request: Request, conn, status: int = 200, error: str | None = None):
    materials = conn.execute(
        "SELECT id, file, year FROM material WHERE kind = 'tasks' ORDER BY id"
    ).fetchall()
    page = TEMPLATES.TemplateResponse(
        request,
        "parent/exam.html",
        {"fmt": exam.get_format(conn), "materials": materials, "stages": exam.STAGES,
         "grades": exam.GRADES, "types": exam.TYPE_LABELS, "error": error},
    )  # fmt: skip
    page.status_code = status
    return page


@parent.get("/parent/exam", response_class=HTMLResponse)
def format_page(request: Request, conn: Db):
    return _format_page(request, conn)


def _number(raw: str, whole: bool):
    text = raw.strip().replace(",", ".")
    if not text:
        return 0
    value = float(text)
    if whole and value != int(value):
        raise ValueError(raw)
    return int(value) if whole else value


@parent.post("/parent/exam")
async def format_save(request: Request, conn: Db):
    form = await request.form()
    try:
        exam.save_format(
            conn, now_of(request),
            stage=str(form.get("stage", "")),
            grade=int(_number(str(form.get("grade", "")), True)),
            minutes=int(_number(str(form.get("minutes", "")), True)),
            counts={t: int(_number(str(form.get(f"count_{t}", "")), True)) for t in exam.TYPES},
            points={t: _number(str(form.get(f"points_{t}", "")), False) for t in exam.TYPES},
            material_ids=[int(m) for m in form.getlist("material") if str(m).isdigit()],
        )  # fmt: skip
    except (ValueError, OverflowError) as exc:
        text = str(exc)
        known = text and not text.startswith(("invalid literal", "could not convert", "cannot"))
        return _format_page(
            request, conn, 422, text if known else "Числа формата — целые (баллы можно с запятой)."
        )
    return RedirectResponse("/parent/exam", status_code=303)
