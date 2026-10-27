"""Карточка рисунка для родителя: вид ребёнка, области на странице, шаги (SPEC-1.0 1.1, #2110).

Только чтение: ни попыток, ни событий, ни запросов модели. Режим подсказок считает та же
assist_mode, что и экран ребёнка."""

import contextlib
import re

import pymupdf

from app.ingest import figures
from app.ingest.publish import (
    assist_caption,
    assist_mode,
    child_view,
    ordered_designations,
)
from app.ingest.store import figure_of, options_of

RASTER_WARNING = (
    "Текст внутри картинок не проверяется автоматически — посмотрите на вырезку глазами."
)
PREVIEW_URL = "/parent/tasks/{}/figure-preview.png"
_LINE = re.compile(r"^\s*(\d{1,2}|[A-ZА-ЯЁ])\s*[:—–-]\s*(.*)$")


def note_lines(note: str | None, items: list[str]) -> tuple[dict[str, str], str]:
    """Описания по обозначениям из teacher_note и остаток, который не разобрался по строкам."""
    notes: dict[str, str] = {}
    rest: list[str] = []
    for piece in re.split(r"[\n;]+", note or ""):
        found = _LINE.match(piece)
        if found and found.group(1) in items and found.group(1) not in notes:
            notes[found.group(1)] = found.group(2).strip()
        elif piece.strip():
            rest.append(piece.strip())
    return notes, "; ".join(rest)


def preview_task(task, figure: dict | None = None, **changes) -> dict:
    """Задание в том виде, как его увидит ребёнок после подтверждения: строка с поправленными
    полями рисунка. Работает на копии, в базу ничего не пишет."""
    import json

    fake = dict(task)
    current = dict(figure if figure is not None else figure_of(task) or {})
    current.update(changes)
    fake["figure"] = json.dumps(current, ensure_ascii=False)
    return fake


def mode_for(task, teacher_note: str | None, ack: bool) -> dict:
    """Режим подсказок для заданных описания и подтверждения: assist_mode на копии."""
    fake = preview_task(task, teacher_note=teacher_note or "", teacher_note_ack=bool(ack))
    return assist_caption(assist_mode(fake))


def child_preview(conn, task, figure: dict, regions: list[dict]) -> tuple[dict, dict]:
    """(вид задания для ребёнка, состояние экрана) по текущему рисунку."""
    material = conn.execute(
        "SELECT year FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    fake = preview_task(task, figure)
    view = child_view(fake, material)
    view["figure"] = (
        {
            "url": PREVIEW_URL.format(task["id"]) + f"?v={task['revision']}",
            "alt": figure.get("alt", ""),
        }
        if regions
        else None
    )
    screen = {
        "terminal": False,
        "correction": None,
        "has_key": task["answer_key"] is not None,
        "repeat": False,
        "last": None,
        "checks_left": 2,
        "hints": 0,
        "assist": assist_mode(fake),
        "solution": None,
        "outcome": None,
    }
    return view, screen


def _overlays(doc, regions: list[dict], shown_page: int) -> list[dict]:
    out = []
    for number, region in enumerate(regions, 1):
        if region["page"] != shown_page:
            continue
        page = doc[region["page"] - 1]
        clip = figures.stored_to_clip(page, region["rect"])
        box = page.rect
        out.append(
            {
                "number": number,
                "left": round(clip.x0 / box.width * 100, 2),
                "top": round(clip.y0 / box.height * 100, 2),
                "width": round(clip.width / box.width * 100, 2),
                "height": round(clip.height / box.height * 100, 2),
            }
        )
    return out


def card_extras(ctx, conn, task, figure: dict, shown_page: int | None, deep: bool) -> dict:
    """Что нужно карточке needs_figure сверх общих полей. deep: открыть PDF (рамки на странице,
    есть ли в них текстовый слой); в общей очереди PDF не открывается."""
    from app.ingest.review import key_info

    regions = figures.regions_of(figure)
    items = ordered_designations(options_of(task))
    notes, rest = note_lines(figure.get("teacher_note"), items)
    view, screen = child_preview(conn, task, figure, regions)
    key, _source, is_candidate = key_info(conn, task)
    extras = {
        "figure_items": items,
        "figure_notes": notes,
        "figure_note_rest": rest,
        "figure_regions": len(regions),
        "child": view,
        "child_screen": screen,
        "mode": assist_caption(screen["assist"]),
        "key_value": key,
        "key_is_candidate": is_candidate,
        "figure_check": {"raster_only": None, "warning": None},
        "overlays": [],
    }
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    source = ctx.material_path(material["file"]) if material else None
    if deep and regions and source is not None and source.exists():
        with contextlib.closing(pymupdf.open(str(source))) as doc:
            raster = not figures.region_lines(doc, regions)
            extras["figure_check"] = {
                "raster_only": raster,
                "warning": RASTER_WARNING if raster else None,
            }
            extras["overlays"] = _overlays(doc, regions, shown_page or regions[0]["page"])
    return extras
