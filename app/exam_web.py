"""Маршруты имитации олимпиады: формат (родитель), сборка, прохождение, сдача и итог (ребёнок)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app import exam
from app.exam import ExamError
from app.ingest.web import TEMPLATES, Db, child_notice_gate
from app.notice import notice_redirect
from app.roles import csrf_token, now_of, require_csrf, require_parent
from app.tutor.web import _needs_notice, child_same_origin, child_same_origin_form

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


def _error(exc: ExamError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": exc.code, "message": exc.message, **exc.extra},
        status_code=exc.status,
    )


def _reader(request: Request):
    """Рисунок условия копируется в снимок при старте."""
    ctx = request.app.state.ctx

    def read(relative: str):
        path = ctx.material_path(relative)
        return path.read_bytes() if path.is_file() else None

    return read


def _state(conn, now) -> dict:
    running = exam.active(conn, now)
    fmt = exam.get_format(conn)
    data = {
        "format": fmt,
        "message": None if fmt else exam.NOT_SET,
        "running_id": running["id"] if running else None,
        "history": exam.history(conn, now),
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
    try:
        attempt_id = exam.start(
            conn, lambda: now_of(request), read_figure=_reader(request), mode=body.mode or "", material_id=body.material_id,
            section=body.section, variant=body.variant, stage=body.stage,
            accept_reduced=body.accept_reduced,
        )  # fmt: skip
    except ExamError as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "attempt_id": attempt_id, "url": "/exam/run"})


@child.get("/api/exam/current", dependencies=[Depends(child_notice_gate)])
def exam_current(request: Request, conn: Db):
    view = exam.current_view(conn, now_of(request))
    if view is None:
        return JSONResponse(
            {"ok": False, "code": "not_running", "message": "Вариант не идёт."}, status_code=404
        )
    return JSONResponse(view)


@child.get("/exam/figure/{position}.png", dependencies=[Depends(child_notice_gate)])
def exam_figure(position: int, request: Request, conn: Db):
    """Рисунок условия из снимка идущего варианта."""
    attempt = exam.active(conn, now_of(request))
    png = exam.figure_png(conn, attempt["id"], position) if attempt else None
    if png is None:
        return HTMLResponse("Рисунка нет", status_code=404)
    return Response(png, media_type="image/png")


def _fill(item: dict, entered: dict | None) -> dict:
    """Что подставить в поля показанного задания: введённое (если действие не выполнено) или
    сохранённое. Выбор и обе стороны соответствия - token варианта, число - текст."""
    if entered is not None:
        return entered
    saved = item["answer"]
    if item["type"] == "numeric":
        return {"n": "" if saved is None else str(saved)}
    if item["type"] == "match":
        side = item["options"]["left"]
        right = {r["token"] for r in item["options"]["right"]}
        pairs = {k: v for k, v in (saved or {}).items() if v in right}
        return {"m": {x["token"]: pairs.get(x["token"], "") for x in side}}
    chosen = [saved] if isinstance(saved, str) else list(saved or [])
    return {"a": chosen}


def _blank(
    request: Request, conn, status: int = 200, message: str | None = None,
    entered: dict | None = None, lost: str | None = None,
):  # fmt: skip
    """Бланк: обычная страница, каждое действие - отправка формы (POST) и 303 на эту страницу."""
    now = now_of(request)
    view = exam.current_view(conn, now)
    if view is None:
        return RedirectResponse("/exam", status_code=303)
    item = view["items"][view["current"] - 1]
    page = TEMPLATES.TemplateResponse(
        request,
        "child/exam_run.html",
        {"v": view, "item": item, "fill": _fill(item, entered), "message": message,
         "lost": lost, "action_id": uuid.uuid4().hex, "type_labels": exam.TYPE_LABELS},
    )  # fmt: skip
    page.status_code = status
    return page


@child.get("/exam/run", response_class=HTMLResponse)
def exam_run(request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect("/exam/run")
    return _blank(request, conn)


def _entered(task_type: str, form) -> tuple[Any, dict, str]:
    """(ответ для проверки, значения для подстановки, текст для показа) из полей формы."""
    if task_type == "numeric":
        text = str(form.get("n", ""))
        return text, {"n": text}, text
    if task_type == "match":
        pairs = {k[2:]: str(form.get(k, "")) for k in form if k.startswith("m.")}
        chosen = {k: v for k, v in pairs.items() if v}
        return chosen, {"m": pairs}, "; ".join(f"{k} → {v}" for k, v in chosen.items())
    chosen = [str(v) for v in form.getlist("a")]
    raw: Any = chosen if task_type == "multiple" else (chosen[0] if chosen else None)
    return raw, {"a": chosen}, ", ".join(chosen)


def _gone(exc: ExamError) -> HTMLResponse:
    return HTMLResponse(
        f'<p role="alert">{exc.message}</p><p><a href="/exam">К олимпиаде</a></p>',
        status_code=exc.status,
    )


@child.post("/exam/act", dependencies=[Depends(child_same_origin_form), Depends(child_notice_gate)])
async def exam_act(request: Request, conn: Db):
    """Действие бланка: «сохранить», переход, «вернусь», «сдать». Успех - 303 на страницу."""
    form = await request.form()
    try:
        attempt_id, rev, shown = (int(str(form.get(k, ""))) for k in ("attempt_id", "rev", "shown"))
        action, _, target = str(form.get("action", "")).partition(":")
        goal = int(target) if target else None
    except ValueError:
        return _gone(ExamError(422, "bad_form", "Форма бланка неполная: открой бланк заново."))
    kind = conn.execute(
        "SELECT task_type FROM exam_item WHERE attempt_id = ? AND position = ?",
        (attempt_id, shown),
    ).fetchone()
    raw, entered, lost = _entered(kind[0] if kind else "single", form)
    # Метка «поля ответа были в форме»: без неё (поля выключены, форма собрана вручную) пустое
    # значение - не «стереть ответ», а «ответ не присылали».
    keep = str(form.get("answer_present", "")) != str(shown)
    try:
        outcome = exam.act(
            conn, lambda: now_of(request), attempt_id=attempt_id, rev=rev,
            action_id=str(form.get("action_id", "")), shown=shown, action=action, target=goal,
            raw=raw, keep_answer=keep,
        )  # fmt: skip
    except ExamError as exc:
        if exc.code == "stale":
            return _blank(request, conn, 409, exc.message, lost=lost or None)
        if exc.code in ("bad_answer", "no_item"):
            return _blank(request, conn, exc.status, exc.message, entered=entered)
        return _gone(exc)
    if outcome in ("submitted", "time"):
        return RedirectResponse(f"/exam/results/{attempt_id}", status_code=303)
    return RedirectResponse("/exam/run", status_code=303)


@child.get("/api/exam/results/{attempt_id}", dependencies=[Depends(child_notice_gate)])
def result_json(attempt_id: int, request: Request, conn: Db):
    exam.expire_due(conn, now_of(request))
    try:
        return JSONResponse(exam.result(conn, attempt_id, now_of(request)))
    except ExamError as exc:
        return _error(exc)


@child.get("/exam/results/{attempt_id}", response_class=HTMLResponse)
def result_page(attempt_id: int, request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect(f"/exam/results/{attempt_id}")
    exam.expire_due(conn, now_of(request))
    try:
        data = exam.result(conn, attempt_id, now_of(request))
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
