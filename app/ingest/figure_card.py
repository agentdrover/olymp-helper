"""Карточка рисунка для родителя: вид ребёнка, области на странице, шаги (SPEC-1.0 1.1, #2110).

Только чтение: ни попыток, ни событий, ни запросов модели. Режим подсказок считает та же
assist_mode, что и экран ребёнка."""

import contextlib
import re
from urllib.parse import urlencode

import pymupdf

from app.ingest import figures, tiles
from app.ingest.answers import display
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
    fake = preview_task(
        task, teacher_note=teacher_note or "", teacher_note_ack=bool(ack), items_v2=True
    )
    return assist_caption(assist_mode(fake))


def child_preview(conn, task, figure: dict, regions: list[dict]) -> tuple[dict, dict]:
    """(вид задания для ребёнка, состояние экрана) по текущему рисунку."""
    material = conn.execute(
        "SELECT year, original_name FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    fake = preview_task(
        task, figure, items_v2=True
    )  # так задание будет оценено после подтверждения
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


def frame_query(frame: dict | None) -> str:
    """Параметры правки рамки для адресов предпросмотра: одни и те же у вырезки и у плиток."""
    frame = frame or {}
    if frame.get("rect_px") is None:
        return ""
    return urlencode(
        {
            "page": frame.get("page") or "",
            "rect_px": ",".join(str(v) for v in frame["rect_px"]),
            "scale": frame.get("scale") or "",
        }
    )


TILE_COUNT_WARNING = (
    "Плиток {tiles}, а рисунков в задании {lefts}: плитки не привязаны по порядку, ответ "
    "вводится для каждого рисунка отдельно."
)


def tile_view(task, tile: dict, number: int, check: dict, query: str) -> dict:
    suffix = f"?v={task['revision']}" + (f"&{query}" if query else "")
    return {
        "id": tile["id"],
        "n": number,
        "page": tile["page"],
        "label": tile.get("label"),
        "url": f"/parent/tasks/{task['id']}/tile-preview/{tile['id']}.png{suffix}",
        "raster_only": check["raster_only"],
        "leak": check["leak"],
        "readable": check["readable"],
        "warning": RASTER_WARNING if check["raster_only"] else None,
    }


def area_context(ctx, conn, task, figure: dict, *, frame=None, state=None) -> dict:
    """Что показывает «Как увидит ребёнок»: плитки (или общая вырезка) по текущей рамке и ввод
    ответа. Один источник и для карточки, и для обновления после правки рамки."""
    from app.ingest.figure_service import edited_page, figure_regions, leak_units, tile_plan
    from app.ingest.review import key_info

    frame = frame or {}
    stored = figures.regions_of(figure)
    key, _source, is_candidate = key_info(conn, task)
    options = options_of(task)
    area = {
        "tiles": [],
        "count": 0,
        "bound": False,
        "warning": None,
        "lefts": tiles.left_labels(options, task["type"]),
        "type": task["type"],
        "options": options,
        "key": key,
        "key_value": key if isinstance(key, dict) else None,
        "key_display": None if key is None else display(key),
        "alt": figure.get("alt", ""),
        "key_is_candidate": is_candidate,
        "common_url": PREVIEW_URL.format(task["id"])
        + f"?v={task['revision']}"
        + (f"&{frame_query(frame)}" if frame_query(frame) else ""),
        "has_regions": bool(stored),
        "raster_only": None,
        "raster_warning": None,
        "overlays": [],
    }
    material = conn.execute(
        "SELECT file FROM material WHERE id = ?", (task["material_id"],)
    ).fetchone()
    source = ctx.material_path(material["file"]) if material else None
    if not (stored and source is not None and source.exists()):
        return area
    with contextlib.closing(pymupdf.open(str(source))) as doc:
        regions = figure_regions(doc, figure, **frame)
        edited = edited_page(figure, **frame)
        plan, lefts = tile_plan(doc, task, figure, regions, edited, state)
        shown = tiles.ordered(plan)
        checks = tiles.check_tiles(doc, shown, leak_units(conn, task))
        query = frame_query(frame)
        area["tiles"] = [
            tile_view(task, t, number, checks[t["id"]], query) for number, t in enumerate(shown, 1)
        ]
        area["count"] = len(shown)
        area["bound"] = bool(lefts) and len(shown) == len(lefts)
        if shown and lefts and len(shown) != len(lefts):
            area["warning"] = TILE_COUNT_WARNING.format(tiles=len(shown), lefts=len(lefts))
        area["has_regions"] = True
        raster = not figures.region_lines(doc, regions)
        area["raster_only"] = raster
        area["raster_warning"] = RASTER_WARNING if raster and not shown else None
        area["page_overlays"] = _overlays(doc, regions, frame.get("page") or regions[0]["page"])
    return area


def card_extras(ctx, conn, task, figure: dict, shown_page: int | None, deep: bool) -> dict:
    """Что нужно карточке needs_figure сверх общих полей. deep: открыть PDF (рамки на странице,
    плитки, есть ли в них текстовый слой); в общей очереди PDF не открывается."""
    regions = figures.regions_of(figure)
    items = ordered_designations(options_of(task))
    notes, rest = note_lines(figure.get("teacher_note"), items)
    view, screen = child_preview(conn, task, figure, regions)
    area = area_context(ctx, conn, task, figure) if deep else None
    key = area["key"] if area else None
    if area is None:
        from app.ingest.review import key_info

        key, _source, is_candidate = key_info(conn, task)
    else:
        is_candidate = area["key_is_candidate"]
    shown_tiles = area["tiles"] if area else []
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
        "area": area,
        "tiles": shown_tiles,
        "tile_warning": area["warning"] if area else None,
        "tile_bound": bool(area and area["bound"]),
        "tiles_raster": sum(1 for t in shown_tiles if t["raster_only"]),
    }
    if area and area["raster_only"] is not None:
        raster = area["raster_only"]
        extras["figure_check"] = {
            "raster_only": raster,
            "warning": RASTER_WARNING if raster else None,
        }
        extras["overlays"] = area.get("page_overlays", [])
    return extras
