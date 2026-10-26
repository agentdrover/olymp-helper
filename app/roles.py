"""Роль запроса: родитель или ребёнок.

Временная защита до входа по PIN (OH-T08): родитель входит по ключу OLYMP_PARENT_KEY через
/parent/login и получает cookie с HMAC этого ключа. T08 заменяет `current_role` и логин.
Ключ не задан - родительского доступа нет вообще.
"""

import hashlib
import hmac
import logging
import os

from fastapi import HTTPException, Request

PARENT = "parent"
CHILD = "child"
COOKIE = "olymp_parent"
log = logging.getLogger("app.roles")


def parent_key() -> str | None:
    return os.environ.get("OLYMP_PARENT_KEY") or None


def parent_token(key: str) -> str:
    return hmac.new(key.encode("utf-8"), b"parent", hashlib.sha256).hexdigest()


def key_matches(candidate: str) -> bool:
    key = parent_key()
    return key is not None and hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8"))


def current_role(request: Request) -> str:
    key = parent_key()
    if key is None:
        return CHILD
    cookie = request.cookies.get(COOKIE, "")
    ok = hmac.compare_digest(cookie.encode("utf-8"), parent_token(key).encode("utf-8"))
    return PARENT if ok else CHILD


def require_parent(request: Request) -> None:
    if parent_key() is None:
        log.warning("OLYMP_PARENT_KEY не задан: родительский доступ закрыт")
        raise HTTPException(status_code=403, detail="Только для родителя")
    if current_role(request) != PARENT:
        # Браузер, открывший страницу родителя без входа, попадает на форму входа;
        # запросы скриптов и API по-прежнему получают 403.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            raise HTTPException(status_code=303, headers={"Location": "/parent/login"})
        raise HTTPException(status_code=403, detail="Только для родителя")
