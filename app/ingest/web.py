"""Маршруты: загрузка и проверка (родитель), список заданий и рисунок (ребёнок)."""

import functools
import hashlib
import sqlite3
from pathlib import Path
from typing import Annotated

import pymupdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.ingest import review
from app.ingest.figure_service import confirm_figure
from app.ingest.limits import MAX_PDF_BYTES, UploadRejected, check_pdf
from app.ingest.publish import child_view, is_published
from app.ingest.review import Refusal
from app.ingest.store import figure_of, get_task, open_db
from app.roles import (
    COOKIE,
    PARENT,
    current_role,
    key_matches,
    parent_key,
    parent_token,
    require_parent,
)
from app.search import search
from app.search.tasks import search_published_tasks

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
KINDS = ("tasks", "key", "textbook")
PAGE_SCALE = 1.5

parent = APIRouter(dependencies=[Depends(require_parent)])
public = APIRouter()


def ctx_of(request: Request):
    return request.app.state.ctx


def db_of(request: Request):
    conn = open_db(ctx_of(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(db_of)]


def _refusal(exc: Refusal) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


# ------------------------------------------------------------------------- вход


@public.get("/parent/login", response_class=HTMLResponse)
def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "parent/login.html", {"error": None})


@public.post("/parent/login")
def login(request: Request, key: Annotated[str, Form()] = ""):
    if not key_matches(key):
        page = TEMPLATES.TemplateResponse(
            request, "parent/login.html", {"error": "Неверный ключ или вход не настроен."}
        )
        page.status_code = 403
        return page
    response = RedirectResponse("/parent/review", status_code=303)
    response.set_cookie(
        COOKIE, parent_token(parent_key()), httponly=True, samesite="strict", path="/"
    )
    return response


# --------------------------------------------------------------------- материалы


@parent.get("/parent/materials", response_class=HTMLResponse)
def materials_page(request: Request, conn: Db):
    rows = conn.execute(
        "SELECT m.*, (SELECT COUNT(*) FROM task_item t WHERE t.material_id = m.id) AS tasks "
        "FROM material m ORDER BY m.id DESC"
    ).fetchall()
    return TEMPLATES.TemplateResponse(request, "parent/materials.html", {"materials": rows})


@parent.post("/parent/materials")
def upload_material(
    request: Request,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "tasks",
    variant: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
    stage: Annotated[str, Form()] = "",
    olympiad: Annotated[str, Form()] = "",
):
    ctx = ctx_of(request)
    if kind not in KINDS:
        return JSONResponse({"error": "Тип материала: задания, ключ или учебник."}, status_code=422)
    if year.strip() and not year.strip().isdigit():
        return JSONResponse({"error": "Год должен быть числом."}, status_code=422)
    data = file.file.read(MAX_PDF_BYTES + 1)
    sha = hashlib.sha256(data).hexdigest()
    conn = open_db(ctx.db_path)
    try:
        existing = conn.execute(
            "SELECT id, import_status FROM material WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing is not None:
            requeued = existing["import_status"] == "failed"
            if requeued:
                conn.execute(
                    "UPDATE material SET import_status = 'queued', import_error = NULL WHERE id = ?",
                    (existing["id"],),
                )
                request.app.state.imports.submit(existing["id"])
            return JSONResponse(
                {"duplicate": True, "requeued": requeued, "material_id": existing["id"]}
            )
        try:
            pages = check_pdf(data)
        except UploadRejected as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        relative = f"materials/{sha}.pdf"
        target = ctx.material_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        cur = conn.execute(
            "INSERT INTO material (file, sha256, kind, olympiad, year, stage, variant, pages_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                relative,
                sha,
                kind,
                olympiad.strip() or None,
                int(year) if year.strip() else None,
                stage.strip() or None,
                variant.strip() or None,
                pages,
            ),
        )
        material_id = cur.lastrowid
    finally:
        conn.close()
    request.app.state.imports.submit(material_id)
    return JSONResponse(
        {"duplicate": False, "material_id": material_id, "status": "queued"}, status_code=202
    )


@parent.get("/parent/materials/{material_id}")
def material_status(material_id: int, conn: Db):
    row = conn.execute(
        "SELECT id, kind, import_status, import_error, pages_total, "
        "(SELECT COUNT(*) FROM task_item t WHERE t.material_id = material.id) AS tasks "
        "FROM material WHERE id = ?",
        (material_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Материал не найден")
    return dict(row)


def _material_file(request: Request, conn, material_id: int) -> Path:
    row = conn.execute("SELECT file, sha256 FROM material WHERE id = ?", (material_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Материал не найден")
    return ctx_of(request).material_path(row["file"]), row["sha256"]


@parent.get("/materials/{material_id}/pdf")
def material_pdf(material_id: int, request: Request, conn: Db):
    path, _ = _material_file(request, conn, material_id)
    return FileResponse(path, media_type="application/pdf")


@parent.get("/materials/{material_id}/pages/{page}.png")
def material_page(material_id: int, page: int, request: Request, conn: Db):
    ctx = ctx_of(request)
    path, sha = _material_file(request, conn, material_id)

    def render() -> bytes:
        doc = pymupdf.open(str(path))
        try:
            if not 1 <= page <= doc.page_count:
                raise HTTPException(404, "Нет такой страницы")
            pix = doc[page - 1].get_pixmap(
                matrix=pymupdf.Matrix(PAGE_SCALE, PAGE_SCALE), alpha=False
            )
            return pix.tobytes("png")
        finally:
            doc.close()

    return Response(
        ctx.page_cache.get(f"{sha}:{page}:{PAGE_SCALE}", render), media_type="image/png"
    )


# ------------------------------------------------------------------------ лента


class ConfirmBody(BaseModel):
    answer: str | None = None


class EditBody(BaseModel):
    text: str | None = None
    options: list | dict | None = None
    teacher_note: str | None = None
    manual_condition: bool = False


class KeyBody(BaseModel):
    answer: str


class ResolveBody(BaseModel):
    choice: str
    answer: str | None = None


class FigureBody(BaseModel):
    rect: list[float] | None = None
    rect_px: list[float] | None = None
    scale: float | None = None
    alt: str = ""
    teacher_note: str | None = None
    no_answer_ack: bool = False
    teacher_note_ack: bool = False


def _advance(request: Request, conn, task_id: int) -> dict:
    card, counter = review.next_card(ctx_of(request), conn, task_id)
    return {"ok": True, "next": card, "counter": counter}


@parent.get("/parent/review", response_class=HTMLResponse)
def review_page(request: Request, conn: Db):
    feed = review.open_feed(ctx_of(request), conn)
    first = feed["cards"][0] if feed["cards"] else None
    counter = f"1 из {feed['total']}" if first else None
    return TEMPLATES.TemplateResponse(
        request, "parent/review.html", {"card": first, "counter": counter, "total": feed["total"]}
    )


@parent.get("/parent/review/queue")
def review_queue(request: Request, conn: Db):
    return review.open_feed(ctx_of(request), conn)


@parent.get("/parent/review/{task_id}", response_class=HTMLResponse)
def review_card(task_id: int, request: Request, conn: Db, fragment: int = 0):
    ctx = ctx_of(request)
    card = review.card_for(ctx, conn, task_id)
    order = ctx.feed.order
    counter = f"{order.index(task_id) + 1} из {len(order)}" if task_id in order else None
    template = "parent/card.html" if fragment else "parent/review.html"
    return TEMPLATES.TemplateResponse(
        request, template, {"card": card, "counter": counter, "total": len(order)}
    )


@parent.get("/parent/tasks/{task_id}")
def task_detail(task_id: int, request: Request, conn: Db):
    try:
        return review.task_detail(ctx_of(request), conn, task_id)
    except Refusal as exc:
        return _refusal(exc)


def _action(handler):
    """Единый разбор отказов: Refusal -> JSON с понятным текстом."""

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Refusal as exc:
            return _refusal(exc)

    return wrapper


@parent.post("/parent/tasks/{task_id}/confirm")
@_action
def task_confirm(task_id: int, request: Request, conn: Db, body: ConfirmBody | None = None):
    review.confirm(ctx_of(request), conn, task_id, (body or ConfirmBody()).answer)
    return _advance(request, conn, task_id)


@parent.post("/parent/tasks/{task_id}/skip")
@_action
def task_skip(task_id: int, request: Request, conn: Db):
    return _advance(request, conn, task_id)


@parent.post("/parent/tasks/{task_id}/edit")
@_action
def task_edit(task_id: int, body: EditBody, conn: Db):
    return review.edit_task(
        conn,
        task_id,
        text=body.text,
        options=body.options,
        teacher_note=body.teacher_note,
        manual_condition=body.manual_condition,
    )


@parent.post("/parent/tasks/{task_id}/key")
@_action
def task_key(task_id: int, body: KeyBody, conn: Db):
    return review.set_parent_key(conn, task_id, body.answer)


@parent.post("/parent/tasks/{task_id}/resolve")
@_action
def task_resolve(task_id: int, body: ResolveBody, request: Request, conn: Db):
    review.resolve(ctx_of(request), conn, task_id, body.choice, body.answer)
    return _advance(request, conn, task_id)


@parent.post("/parent/tasks/{task_id}/exclude")
@_action
def task_exclude(task_id: int, request: Request, conn: Db):
    review.exclude_task(conn, task_id)
    return _advance(request, conn, task_id)


@parent.post("/parent/tasks/{task_id}/retry-model")
@_action
def task_retry(task_id: int, request: Request, conn: Db):
    return {"ok": review.retry_model(ctx_of(request), conn, task_id)}


@parent.post("/parent/tasks/{task_id}/figure")
@_action
def task_figure(task_id: int, body: FigureBody, request: Request, conn: Db):
    return confirm_figure(
        ctx_of(request),
        conn,
        task_id,
        rect=body.rect,
        rect_px=body.rect_px,
        scale=body.scale,
        alt=body.alt,
        teacher_note=body.teacher_note,
        no_answer_ack=body.no_answer_ack,
        teacher_note_ack=body.teacher_note_ack,
    )


# ----------------------------------------------------------------------- ребёнок


def _published(conn, task_id: int):
    task = get_task(conn, task_id)
    if task is None or not is_published(task):
        raise HTTPException(404, "Задание не найдено")
    material = conn.execute(
        "SELECT year FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    return task, material


@public.get("/api/tasks")
def api_tasks(conn: Db):
    out = []
    for task in conn.execute("SELECT * FROM task_item ORDER BY id").fetchall():
        if is_published(task):
            material = conn.execute(
                "SELECT year FROM material WHERE id = ?", (task["material_id"],)
            ).fetchone()
            out.append(child_view(task, material))
    return {"tasks": out}


@public.get("/api/tasks/{task_id}")
def api_task(task_id: int, conn: Db):
    task, material = _published(task_id=task_id, conn=conn)
    return child_view(task, material)


@public.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, conn: Db):
    return TEMPLATES.TemplateResponse(request, "child/tasks.html", api_tasks(conn))


@public.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(task_id: int, request: Request, conn: Db):
    task, material = _published(task_id=task_id, conn=conn)
    return TEMPLATES.TemplateResponse(
        request, "child/task.html", {"task": child_view(task, material)}
    )


@public.get("/tasks/{task_id}/figure.png")
def task_figure_png(task_id: int, request: Request, conn: Db):
    task = get_task(conn, task_id)
    figure = figure_of(task) if task is not None else None
    if figure is None or not figure.get("confirmed") or not figure.get("file"):
        raise HTTPException(404, "Рисунка нет")
    if current_role(request) != PARENT and not is_published(task):
        raise HTTPException(404, "Рисунка нет")
    return FileResponse(ctx_of(request).material_path(figure["file"]), media_type="image/png")


@public.get("/api/search")
def api_search(request: Request, conn: Db, q: str = ""):
    """Родитель ищет по страницам материалов; ребёнок - только по опубликованным заданиям."""
    if current_role(request) == PARENT:
        return {"results": search(conn, q)}
    return {"results": search_published_tasks(conn, q)}
