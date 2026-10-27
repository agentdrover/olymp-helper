"""Итоги занятий: «Мои итоги» ребёнка, «Итоги занятий» родителя, «Закончить», «Показать родителю»,
просмотр переписки родителем (OH-T10).

Ребёнок: уведомление о внешней модели и JSON из своего источника (как T06/T07). Родитель: сессия
по PIN и CSRF (T08)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import privacy, summary
from app.ingest.web import TEMPLATES, Db, child_notice_gate
from app.notice import notice_redirect
from app.roles import now_of, require_csrf, require_parent
from app.tutor.web import _needs_notice, child_same_origin

child = APIRouter()
parent = APIRouter(dependencies=[Depends(require_parent), Depends(require_csrf)])


# ----------------------------------------------------------------------- ребёнок


def _my_data(conn, now, selected_id: int | None) -> dict:
    sessions = summary.list_sessions(conn, now)
    done = [s for s in sessions if s["summary"]]
    if selected_id is None and done:
        selected_id = done[0]["id"]
    selected = next((s["summary"] for s in done if s["id"] == selected_id), None)
    return {
        "sessions": [
            {k: v for k, v in s.items() if k != "summary"}
            | {"summary": None if s["summary"] is None else _brief(s["summary"])}
            for s in sessions
        ],
        "selected_id": selected_id if selected else None,
        "selected": selected,
        "revisit": summary.overall_revisit(sessions),
        "journal": summary.journal(conn),
        "shown_ids": sorted(
            j["session_id"] for j in summary.journal(conn) if j["type"] == "shown_to_parent"
        ),
    }


def _brief(s: dict) -> dict:
    return {"attempts": s["attempts"], "conclusion": s["conclusion"]}


@child.get("/my", response_class=HTMLResponse)
def my_page(request: Request, conn: Db, session: int | None = None):
    if _needs_notice(request, conn):
        return notice_redirect("/my")
    data = _my_data(conn, now_of(request), session)
    return TEMPLATES.TemplateResponse(request, "child/summaries.html", {"d": data})


@child.get("/how", response_class=HTMLResponse)
def how_page(request: Request, conn: Db):
    if _needs_notice(request, conn):
        return notice_redirect("/how")
    return TEMPLATES.TemplateResponse(request, "child/how.html", {})


@child.get("/api/my", dependencies=[Depends(child_notice_gate)])
def my_json(request: Request, conn: Db, session: int | None = None):
    return JSONResponse(_my_data(conn, now_of(request), session))


@child.get("/api/my/sessions/{session_id}", dependencies=[Depends(child_notice_gate)])
def my_summary_json(session_id: int, request: Request, conn: Db):
    found = summary.get_summary(conn, session_id, now_of(request))
    if found is None:
        raise HTTPException(404, "Итога этого занятия нет")
    return JSONResponse(found)


@child.post(
    "/session/finish", dependencies=[Depends(child_same_origin), Depends(child_notice_gate)]
)
def finish(request: Request, conn: Db):
    """«Закончить»: занятие закрывается сейчас, итог строится сразу."""
    built = summary.finish(conn, now_of(request))
    if built is None:
        return JSONResponse(
            {"ok": False, "code": "no_session", "message": "Идущего занятия нет."},
            status_code=409,
        )
    return JSONResponse({"ok": True, "session_id": built["session_id"], "summary": built})


@child.post(
    "/my/sessions/{session_id}/show-parent",
    dependencies=[Depends(child_same_origin), Depends(child_notice_gate)],
)
def show_parent(session_id: int, request: Request, conn: Db):
    if not summary.session_exists(conn, session_id):
        raise HTTPException(404, "Занятие не найдено")
    added = summary.show_to_parent(conn, session_id, now_of(request))
    return JSONResponse({"ok": True, "session_id": session_id, "recorded": added})


# ----------------------------------------------------------------------- родитель


def _list(request: Request, conn, selected_id: int | None):
    now = now_of(request)
    sessions = summary.list_sessions(conn, now)
    messages = {r["id"]: r["messages"] for r in privacy.conversations(conn)}
    selected = None
    if selected_id is not None:
        selected = summary.get_summary(conn, selected_id, now)
        if selected is None:
            raise HTTPException(404, "Итога этого занятия нет")
    return TEMPLATES.TemplateResponse(
        request,
        "parent/summaries.html",
        {"sessions": sessions, "messages": messages, "selected": selected,
         "selected_id": selected_id},
    )  # fmt: skip


@parent.get("/parent/summaries", response_class=HTMLResponse)
def summaries_page(request: Request, conn: Db):
    return _list(request, conn, None)


@parent.get("/parent/summaries/{session_id}", response_class=HTMLResponse)
def summary_page(session_id: int, request: Request, conn: Db):
    return _list(request, conn, session_id)


def _chat_page(request: Request, conn, session_id: int):
    """Фактический просмотр: страница с перепиской и ровно одна запись parent_opened_chat."""
    shown = summary.shown_by_child(conn, session_id)
    summary.log_parent_open(conn, session_id, now_of(request))
    return TEMPLATES.TemplateResponse(
        request,
        "parent/session_chat.html",
        {"session_id": session_id, "messages": summary.chat_of(conn, session_id), "shown": shown},
    )


@parent.get("/parent/sessions/{session_id}/chat", response_class=HTMLResponse)
def chat_open(session_id: int, request: Request, conn: Db):
    if not summary.session_exists(conn, session_id):
        raise HTTPException(404, "Занятие не найдено")
    if summary.shown_by_child(conn, session_id):
        return _chat_page(request, conn, session_id)  # ребёнок показал сам: без подтверждения
    return TEMPLATES.TemplateResponse(
        request, "parent/chat_confirm.html", {"session_id": session_id}
    )


@parent.post("/parent/sessions/{session_id}/chat", response_class=HTMLResponse)
def chat_confirmed(
    session_id: int, request: Request, conn: Db, confirm: Annotated[str, Form()] = ""
):
    if not summary.session_exists(conn, session_id):
        raise HTTPException(404, "Занятие не найдено")
    if not confirm and not summary.shown_by_child(conn, session_id):
        return TEMPLATES.TemplateResponse(
            request, "parent/chat_confirm.html", {"session_id": session_id}
        )
    return _chat_page(request, conn, session_id)
