"""Уведомление ребёнку о внешней модели (SPEC 1.6): показано ли оно и куда вернуть после него."""

from urllib.parse import quote

from fastapi.responses import RedirectResponse


def notice_seen(conn) -> bool:
    row = conn.execute("SELECT child_notice_seen_at FROM settings WHERE id = 1").fetchone()
    return row is not None and row[0] is not None


def safe_next(target: str) -> str:
    """Только путь на этом же сайте: «//host» и «/\\host» браузер читает как чужой адрес."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/tasks"


def notice_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"/notice?next={quote(path, safe='/')}", status_code=303)
