"""Роль запроса: родитель или ребёнок (OH-T08).

Ребёнок входит без пароля. Родитель - по PIN: cookie хранит случайный id серверной сессии
(app.auth), сессия живёт до 30 минут простоя. Каждый родительский маршрут проверяется на сервере:
`require_parent` стоит зависимостью на всём родительском роутере, `require_csrf` - на всех
изменяющих запросах. OLYMP_PARENT_KEY нужен только для первичной установки PIN.
"""

import hmac
import logging
import os
from datetime import UTC, datetime

from fastapi import HTTPException, Request

from app import auth
from app.ingest.store import open_db

PARENT = "parent"
CHILD = "child"
COOKIE = "olymp_parent"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
log = logging.getLogger("app.roles")


def parent_key() -> str | None:
    return os.environ.get("OLYMP_PARENT_KEY") or None


def key_matches(candidate: str) -> bool:
    key = parent_key()
    return key is not None and hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8"))


def now_of(request: Request) -> datetime:
    return request.app.state.clock().astimezone(UTC)


def _csrf_of_session(request: Request) -> str | None:
    """csrf-токен живой сессии запроса или None. Один поиск на запрос; поиск обновляет простой."""
    if not hasattr(request.state, "parent_csrf"):
        sid = request.cookies.get(COOKIE, "")
        token = None
        if sid:
            conn = open_db(request.app.state.ctx.db_path)
            try:
                token = auth.lookup_session(conn, now_of(request), sid)
            finally:
                conn.close()
        request.state.parent_csrf = token
    return request.state.parent_csrf


def current_role(request: Request) -> str:
    return PARENT if _csrf_of_session(request) is not None else CHILD


def csrf_token(request: Request) -> str:
    """Токен для форм и скриптов страниц родителя; пусто, если сессии нет."""
    return _csrf_of_session(request) or ""


def require_parent(request: Request) -> None:
    if current_role(request) != PARENT:
        # Браузер, открывший страницу родителя без входа, попадает на экран PIN;
        # запросы скриптов и API по-прежнему получают 403.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            raise HTTPException(status_code=303, headers={"Location": "/parent/login"})
        raise HTTPException(status_code=403, detail="Только для родителя")


async def require_csrf(request: Request) -> None:
    """Изменяющий запрос родителя несёт токен своей сессии: заголовком или полем формы."""
    if request.method in SAFE_METHODS:
        return
    expected = _csrf_of_session(request)
    given = request.headers.get(CSRF_HEADER)
    if given is None:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            given = (await request.form()).get(CSRF_FIELD)
    ok = (
        expected is not None
        and isinstance(given, str)
        and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Нет или неверен токен защиты формы")
