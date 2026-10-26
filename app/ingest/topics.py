"""Каталог тем: чтение из БД, список для запроса к модели и проверка кода темы."""

CATALOG_HEADER = (
    "Тема задания (поле topic) - код из каталога ниже. Если ни одна тема не подходит или ты "
    "не уверен, поле topic не заполняй. Кодов вне каталога не придумывай.\nКаталог тем:"
)


def load_catalog(conn) -> dict[str, dict]:
    """code -> {code, title, parent_code}; порядок как в каталоге."""
    rows = conn.execute(
        "SELECT code, title, parent_code FROM topic ORDER BY position, code"
    ).fetchall()
    return {r[0]: {"code": r[0], "title": r[1], "parent_code": r[2]} for r in rows}


def catalog_prompt(catalog: dict[str, dict]) -> str:
    lines = [f"{t['code']} - {t['title']}" for t in catalog.values()]
    return CATALOG_HEADER + "\n" + "\n".join(lines)


def accept(value, catalog) -> str | None:
    """Код из каталога или None: предложение вне каталога отбрасывается."""
    if isinstance(value, str) and value.strip() in catalog:
        return value.strip()
    return None
