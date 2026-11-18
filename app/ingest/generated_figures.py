"""Explicit generated provenance for a parent-supplied PDF and structured visual exercises."""

import hashlib
import json
import math

import pymupdf

from app.ingest.answers import key_json
from app.ingest.figure_pipeline import _persist_file
from app.ingest.figures import stored_to_clip
from app.ingest.limits import check_pdf
from app.ingest.names import clean_original_name
from app.ingest.packs import check_item
from app.ingest.schema import InvalidStructure
from app.ingest.store import atomic, now, save_snapshot
from app.ingest.topics import load_catalog


def import_package(ctx, conn, pdf: bytes, questions: list, filename: str) -> dict:
    pages = check_pdf(pdf)
    if not isinstance(questions, list) or not 1 <= len(questions) <= 20:
        raise ValueError("Нужны от 1 до 20 авторских заданий.")
    catalog = load_catalog(conn)
    prepared, numbers = [], set()
    with pymupdf.open("pdf", pdf) as doc:
        for n, raw in enumerate(questions, 1):
            if not isinstance(raw, dict) or raw.get("type") not in (
                "single",
                "multiple",
                "match",
                "numeric",
            ):
                raise ValueError("Неподдерживаемый тип авторского задания.")
            try:
                question = check_item(raw, raw["type"], allow_figure=True)
            except InvalidStructure as exc:
                raise ValueError(str(exc)) from None
            number = str(raw.get("id") or n).strip()
            if not number or len(number) > 64 or number in numbers:
                raise ValueError("Номера авторских заданий пусты или повторяются.")
            numbers.add(number)
            page, rect, topic = raw.get("page"), raw.get("rect"), raw.get("topic")
            if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= pages:
                raise ValueError("Страница рисунка отсутствует в PDF.")
            if (
                not isinstance(rect, list)
                or len(rect) != 4
                or any(
                    isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x)
                    for x in rect
                )
            ):
                raise ValueError("Нужна конечная прямоугольная рамка рисунка.")
            clip = stored_to_clip(doc[page - 1], rect)
            if clip.is_empty or not doc[page - 1].rect.contains(clip):
                raise ValueError("Рамка выходит за страницу.")
            if topic not in catalog:
                raise ValueError("Тема авторского задания не найдена в каталоге.")
            prepared.append(
                {"number": number, "question": question, "page": page, "rect": rect, "topic": topic}
            )
    sha = hashlib.sha256(pdf).hexdigest()
    payload_sha = hashlib.sha256(
        json.dumps(prepared, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with atomic(conn):
        existing = conn.execute(
            "SELECT id,deleted_at FROM material WHERE sha256=?", (sha,)
        ).fetchone()
        if existing:
            previous = conn.execute(
                "SELECT payload_sha256 FROM generated_figure_package WHERE material_id=?",
                (existing["id"],),
            ).fetchone()
            if existing["deleted_at"] or not previous or previous[0] != payload_sha:
                raise ValueError(
                    "Этот PDF уже загружен с другими данными; прежние задания сохранены."
                )
            material_id, duplicate = existing["id"], True
        else:
            relative = f"materials/{sha}.pdf"
            _persist_file(ctx, relative, pdf)
            material_id = conn.execute(
                "INSERT INTO material(file,sha256,kind,olympiad,stage,import_status,pages_total,"
                "original_name) VALUES(?,?,'tasks','generated','авторские','done',?,?)",
                (relative, sha, pages, clean_original_name(filename)),
            ).lastrowid
            conn.execute(
                "INSERT INTO generated_figure_package VALUES(?,?)", (material_id, payload_sha)
            )
            for item in prepared:
                q, page, rect = item["question"], item["page"], item["rect"]
                figure = {
                    "page": page,
                    "rect": rect,
                    "regions": [{"page": page, "rect": rect}],
                    "suggested": True,
                    "confirmed": False,
                    "ambiguous": False,
                    "anchor_unknown": False,
                }
                tid = conn.execute(
                    "INSERT INTO task_item(material_id,source_number,type,text,options,topic,"
                    "page_from,page_to,text_page,figure,import_status,unsupported_reason,"
                    "answer_key,key_source,parent_note) VALUES(?,?,?,?,?,?,?,?,?,?,'unsupported',"
                    "'Рисунок ожидает независимой проверки',?,'generated',?)",
                    (
                        material_id,
                        item["number"],
                        q["type"],
                        q["text"],
                        json.dumps(q["options"], ensure_ascii=False) if q["options"] else None,
                        item["topic"],
                        page,
                        page,
                        page,
                        json.dumps(figure, ensure_ascii=False),
                        key_json(q["value"], q["type"]),
                        q["explanation"],
                    ),
                ).lastrowid
                save_snapshot(conn, tid)
            duplicate = False
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM task_item WHERE material_id=? ORDER BY id", (material_id,)
            )
        ]
    return {
        "material_id": material_id,
        "task_ids": ids,
        "duplicate": duplicate,
        "created_at": now(),
    }
