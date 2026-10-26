"""Вход и профили (OH-T08): экран PIN, первичная установка, выход, уведомление ребёнку,
настройки, удаление переписки и материалов."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import auth, privacy
from app.ingest.web import TEMPLATES, Db, ctx_of
from app.notice import safe_next
from app.roles import COOKIE, key_matches, now_of, parent_key, require_csrf, require_parent

parent = APIRouter(dependencies=[Depends(require_parent), Depends(require_csrf)])
public = APIRouter()


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n // 10 % 10 != 1:
        return few
    return many


TEMPLATES.env.globals["plural"] = plural


# ------------------------------------------------------------------------- вход


def _login_page(request: Request, conn, *, status=200, error=None, verdict=None):
    now = now_of(request)
    pin_set = auth.pin_is_set(conn)
    until = verdict.until if verdict is not None and verdict.kind == "locked" else None
    if until is None:
        until = auth.locked_until(conn, now)
    left = auth.failures_left(conn)
    if error is None and until is None and left < auth.MAX_FAILURES and pin_set:
        error = f"Неверный PIN. Осталось попыток: {left}."
    if until is not None:
        error = f"Вход закрыт до {auth.local_time(until)}: 5 неверных PIN подряд."
    page = TEMPLATES.TemplateResponse(
        request,
        "parent/login.html",
        {
            "pin_set": pin_set,
            "key_configured": parent_key() is not None,
            "error": error,
            "locked": until is not None,
            "max_failures": auth.MAX_FAILURES,
        },
    )
    page.status_code = status
    if until is not None:
        page.headers["Retry-After"] = str(max(1, int((until - now).total_seconds())))
    return page


def _start_session(request: Request, conn, target: str = "/parent/review"):
    sid, _ = auth.create_session(conn, now_of(request))
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        sid,
        httponly=True,
        samesite="strict",
        path="/",
        secure=request.url.scheme == "https",
    )
    return response


@public.get("/parent/login", response_class=HTMLResponse)
def login_page(request: Request, conn: Db):
    return _login_page(request, conn)


@public.post("/parent/login")
def login(request: Request, conn: Db, pin: Annotated[str, Form()] = ""):
    verdict = auth.attempt_pin(conn, now_of(request), pin)
    if verdict.kind == "ok":
        return _start_session(request, conn)
    if verdict.kind == "not_set":
        return _login_page(request, conn, status=403)
    if verdict.kind == "locked":
        return _login_page(request, conn, status=429, verdict=verdict)
    return _login_page(
        request, conn, status=401, error=f"Неверный PIN. Осталось попыток: {verdict.left}."
    )


@public.post("/parent/setup")
def setup(
    request: Request,
    conn: Db,
    key: Annotated[str, Form()] = "",
    pin: Annotated[str, Form()] = "",
    pin2: Annotated[str, Form()] = "",
):
    """Первичная установка PIN по ключу OLYMP_PARENT_KEY. После установки закрыта навсегда."""
    if parent_key() is None:
        return _login_page(request, conn, status=403)
    if auth.pin_is_set(conn):
        return _login_page(request, conn, status=403)
    if not auth.valid_pin(pin) or pin != pin2:
        return _login_page(
            request, conn, status=422, error="PIN — ровно четыре цифры, оба поля одинаковые."
        )
    verdict = auth.setup_pin(conn, now_of(request), key_matches(key), pin)
    if verdict.kind == "ok":
        return _start_session(request, conn)
    if verdict.kind == "locked":
        return _login_page(request, conn, status=429, verdict=verdict)
    return _login_page(request, conn, status=403, error="Неверный ключ.")


@parent.post("/parent/logout")
def logout(request: Request, conn: Db):
    """Выход родителя: сессия стирается на сервере, cookie - у браузера."""
    auth.end_session(conn, request.cookies.get(COOKIE, ""))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return response


# ------------------------------------------------------------- уведомление ребёнку


@public.get("/notice", response_class=HTMLResponse)
def notice_page(request: Request, next: str = "/tasks"):
    return TEMPLATES.TemplateResponse(request, "notice.html", {"next": safe_next(next)})


@public.post("/notice/ack")
def notice_ack(request: Request, conn: Db, next: Annotated[str, Form()] = "/tasks"):
    """Ребёнок прочитал уведомление; первый момент не перезаписывается."""
    conn.execute(
        "UPDATE settings SET child_notice_seen_at = COALESCE(child_notice_seen_at, ?) WHERE id = 1",
        (now_of(request).isoformat(),),
    )
    return RedirectResponse(safe_next(next), status_code=303)


# ---------------------------------------------------------------------- настройки


def _settings_page(request: Request, conn, status=200, error=None):
    row = conn.execute(
        "SELECT show_exam_countdown, stage_date FROM settings WHERE id = 1"
    ).fetchone()
    page = TEMPLATES.TemplateResponse(
        request, "parent/settings.html", {"settings": row, "error": error}
    )
    page.status_code = status
    return page


@parent.get("/parent/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: Db):
    return _settings_page(request, conn)


@parent.post("/parent/settings")
def settings_save(
    request: Request,
    conn: Db,
    show_exam_countdown: Annotated[str, Form()] = "",
    stage_date: Annotated[str, Form()] = "",
):
    stage = stage_date.strip()
    if stage:
        try:
            if date.fromisoformat(stage).isoformat() != stage:
                raise ValueError(stage)
        except ValueError:
            return _settings_page(request, conn, 422, "Дата этапа — в виде ГГГГ-ММ-ДД.")
    conn.execute(
        "UPDATE settings SET show_exam_countdown = ?, stage_date = ? WHERE id = 1",
        (1 if show_exam_countdown else 0, stage or None),
    )
    return RedirectResponse("/parent/settings", status_code=303)


# ------------------------------------------------------------------- переписка


def _confirm(request: Request, *, heading, message, action, expected, back, danger="Удалить"):
    return TEMPLATES.TemplateResponse(
        request,
        "parent/confirm.html",
        {
            "heading": heading,
            "message": message,
            "action": action,
            "expected": expected,
            "back": back,
            "danger": danger,
        },
    )


def _messages_word(n: int) -> str:
    return f"{n} {plural(n, 'сообщение', 'сообщения', 'сообщений')}"


@parent.get("/parent/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, conn: Db):
    return TEMPLATES.TemplateResponse(
        request,
        "parent/conversations.html",
        {"sessions": privacy.conversations(conn), "total": privacy.message_count(conn)},
    )


def _conversation_confirm(request: Request, conn, session_id: int | None):
    count = privacy.message_count(conn, session_id)
    if session_id is None:
        heading, action = "Удалить всю переписку", "/parent/conversations/delete"
        message = f"Будет удалено {_messages_word(count)} из всех занятий."
    else:
        heading = f"Удалить переписку занятия №{session_id}"
        action = f"/parent/conversations/{session_id}/delete"
        message = f"Будет удалено {_messages_word(count)} этого занятия."
    message += " Итоги занятий останутся. Отменить удаление нельзя."
    return _confirm(
        request,
        heading=heading,
        message=message,
        action=action,
        expected=count,
        back="/parent/conversations",
    )


def _delete_conversation(request: Request, conn, session_id, confirm, expected):
    if session_id is not None and not privacy.session_exists(conn, session_id):
        raise HTTPException(404, "Занятие не найдено")
    # Подтверждение действует на то число, которое родитель увидел: если сообщений стало
    # больше или меньше, вопрос задаётся заново.
    if not confirm or expected != privacy.message_count(conn, session_id):
        return _conversation_confirm(request, conn, session_id)
    privacy.delete_messages(conn, session_id)
    return RedirectResponse("/parent/conversations", status_code=303)


@parent.get("/parent/conversations/delete", response_class=HTMLResponse)
def conversations_delete_page(request: Request, conn: Db):
    return _conversation_confirm(request, conn, None)


@parent.post("/parent/conversations/delete")
def conversations_delete(
    request: Request,
    conn: Db,
    confirm: Annotated[str, Form()] = "",
    expected: Annotated[int, Form()] = -1,
):
    return _delete_conversation(request, conn, None, confirm, expected)


@parent.get("/parent/conversations/{session_id}/delete", response_class=HTMLResponse)
def conversation_delete_page(session_id: int, request: Request, conn: Db):
    if not privacy.session_exists(conn, session_id):
        raise HTTPException(404, "Занятие не найдено")
    return _conversation_confirm(request, conn, session_id)


@parent.post("/parent/conversations/{session_id}/delete")
def conversation_delete(
    session_id: int,
    request: Request,
    conn: Db,
    confirm: Annotated[str, Form()] = "",
    expected: Annotated[int, Form()] = -1,
):
    return _delete_conversation(request, conn, session_id, confirm, expected)


# -------------------------------------------------------------------- материалы


def _material_confirm(request: Request, conn, material_id: int):
    info = privacy.material_summary(conn, material_id)
    if info is None:
        raise HTTPException(404, "Материал не найден")
    u, c, p = info["unconfirmed"], info["confirmed"], info["pages"]
    parts = [
        "Будут удалены файл материала",
        f"{p} {plural(p, 'страница', 'страницы', 'страниц')} для поиска",
        f"{u} {plural(u, 'неподтверждённое задание', 'неподтверждённых задания', 'неподтверждённых заданий')}.",
    ]
    message = ", ".join(parts[:2]) + " и " + parts[2]
    if c:
        message += (
            f" {c} {plural(c, 'подтверждённое задание останется', 'подтверждённых задания останутся', 'подтверждённых заданий останутся')}"
            " — ребёнок мог их решать."
        )
    message += " Отменить удаление нельзя."
    return _confirm(
        request,
        heading=f"Удалить материал №{material_id}",
        message=message,
        action=f"/parent/materials/{material_id}/delete",
        expected=0,
        back="/parent/materials",
    )


@parent.get("/parent/materials/{material_id}/delete", response_class=HTMLResponse)
def material_delete_page(material_id: int, request: Request, conn: Db):
    return _material_confirm(request, conn, material_id)


@parent.post("/parent/materials/{material_id}/delete")
def material_delete(
    material_id: int,
    request: Request,
    conn: Db,
    confirm: Annotated[str, Form()] = "",
):
    if not confirm:
        return _material_confirm(request, conn, material_id)
    try:
        privacy.delete_material(ctx_of(request), conn, material_id)
    except LookupError:
        raise HTTPException(404, "Материал не найден") from None
    except privacy.MaterialBusy:
        return JSONResponse(
            {"error": "Импорт этого материала ещё идёт. Удалите его, когда он закончится."},
            status_code=409,
        )
    return RedirectResponse("/parent/materials", status_code=303)
