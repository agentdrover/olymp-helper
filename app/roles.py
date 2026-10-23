"""Роль запроса: родитель или ребёнок.

Пока нет входа по PIN (OH-T08), роль задаёт cookie `olymp_role`; без неё запрос считается
детским. T08 заменяет только `current_role`: все маршруты зависят от `require_parent`.
"""

from fastapi import HTTPException, Request

PARENT = "parent"
CHILD = "child"


def current_role(request: Request) -> str:
    return PARENT if request.cookies.get("olymp_role") == PARENT else CHILD


def require_parent(request: Request) -> None:
    if current_role(request) != PARENT:
        raise HTTPException(status_code=403, detail="Только для родителя")
