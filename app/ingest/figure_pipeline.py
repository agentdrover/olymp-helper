"""Publish figure revisions only after blind, independent image and description checks."""

import hashlib
import json
import math
import os
import tempfile
from copy import deepcopy

import pymupdf

from app.ingest import figure_models, figure_proof, figures, sampling, tiles
from app.ingest.answers import key_json, key_value
from app.ingest.figure_models import FigureDoubt
from app.ingest.keys import KeySourceUnavailable, official_values
from app.ingest.page_answer import check_answer
from app.ingest.schema import InvalidStructure, check_options
from app.ingest.store import (
    atomic,
    figure_of,
    get_task,
    now,
    options_of,
    save_snapshot,
    update_task,
)
from app.ingest.topics import load_catalog
from app.model.images import MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE, MAX_IMAGES

HOLD = "Проверка рисунка: "
HOLD_SAMPLE = HOLD + "выбрано для вашей проверки до публикации."
HOLD_KEYLESS = HOLD + "ключа сборника нет; сравните решения двух моделей и подтвердите ответ."
HOLD_STALE = HOLD + "задание или исходные данные изменились во время проверки; нужен новый проход."


def _eligible(task):
    return bool(
        task
        and not task["excluded"]
        and task["type"] != "open"
        and task["import_status"] in ("draft", "unsupported")
        and figure_of(task)
    )


def _prepare(ctx, conn, task):
    from app.ingest.figure_service import figure_regions, leak_units, tile_plan

    figure = figure_of(task)
    if figure.get("ambiguous") or figure.get("anchor_unknown"):
        raise FigureDoubt("рамка рисунка неоднозначна; поправьте её на странице")
    material = conn.execute("SELECT * FROM material WHERE id=?", (task["material_id"],)).fetchone()
    if material is None or material["deleted_at"] is not None:
        raise FigureDoubt("исходный материал недоступен")
    source = ctx.material_path(material["file"])
    try:
        pdf = source.read_bytes()
    except OSError:
        raise FigureDoubt("исходный файл рисунка недоступен") from None
    source_hash = hashlib.sha256(pdf).hexdigest()
    if source_hash != material["sha256"]:
        raise FigureDoubt("исходный файл изменился; загрузите материал заново")
    try:
        with pymupdf.open("pdf", pdf) as doc:
            regions = figure_regions(doc, figure)
            sizes = []
            for region in regions:
                rect = region["rect"]
                if len(rect) != 4 or not all(math.isfinite(float(x)) for x in rect):
                    raise FigureDoubt("рамка рисунка задана неверно")
                page = doc[region["page"] - 1]
                clip = figures.stored_to_clip(page, rect)
                if clip.is_empty or not page.rect.contains(clip):
                    raise FigureDoubt("рамка выходит за границы страницы")
                sizes.append(
                    (math.ceil(clip.width * figures.SCALE), math.ceil(clip.height * figures.SCALE))
                )
            width, height = max(w for w, _ in sizes), sum(h for _, h in sizes)
            if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                raise FigureDoubt("рисунок слишком большой для проверки моделями")
            plan, lefts = tile_plan(doc, task, figure, regions, None)
            if len(plan["tiles"]) + 1 > MAX_IMAGES:
                raise FigureDoubt("рисунок слишком большой: слишком много отдельных плиток")
            if tiles.check_labels(plan, lefts):
                raise FigureDoubt("плитки рисунка не сопоставлены с обозначениями задания")
            units = leak_units(conn, task)
            checks = tiles.check_tiles(doc, tiles.ordered(plan), units)
            for tile in tiles.ordered(plan):
                checks[tile["id"]]["leak"] = figure_proof.crop_leaks(
                    figures.region_lines(doc, [{"page": tile["page"], "rect": tile["rect"]}]), units
                )
            if not figures.regions_readable(doc, regions) or any(
                not c["readable"] for c in checks.values()
            ):
                raise FigureDoubt("подписи рисунка слишком мелкие")
            if figure_proof.crop_leaks(figures.region_lines(doc, regions), units) or any(
                c["leak"] for c in checks.values()
            ):
                raise FigureDoubt("в вырезке напечатан ответ или текст варианта")
            common = figures.render_regions_png(doc, regions)
            parts = [tiles.render_tile_png(doc, tile) for tile in tiles.ordered(plan)]
    except FigureDoubt:
        raise
    except (OSError, ValueError, TypeError, IndexError, KeyError, RuntimeError):
        raise FigureDoubt("не удалось прочитать рамку рисунка") from None
    return {
        "regions": regions,
        "plan": plan,
        "common": common,
        "parts": parts,
        "source": source,
        "source_hash": source_hash,
    }


def _key(ctx, conn, task, key_cache=None):
    try:
        check_options(task["type"], options_of(task))
    except InvalidStructure:
        raise FigureDoubt("структура вариантов требует проверки") from None
    if task["review_note"]:
        raise FigureDoubt("при разборе задания остались замечания")
    if task["topic"] not in load_catalog(conn):
        raise FigureDoubt("выберите тему задания из каталога")
    generated = task["key_source"] == "generated"
    try:
        values = (
            [key_value(task["answer_key"])]
            if generated
            else official_values(ctx, conn, task, key_cache if key_cache is not None else {})
        )
    except KeySourceUnavailable:
        raise FigureDoubt("не удалось перепроверить исходный файл ключа") from None
    if None in values or len(values) > 1:
        raise FigureDoubt("ключ сборника неполон или содержит разные ответы")
    value = values[0] if values else None
    if value is not None and check_answer(value, task["type"], options_of(task)):
        raise FigureDoubt("ключ не соответствует всем вариантам задания")
    if task["answer_key"] is not None:
        if task["key_source"] not in ("official", "generated"):
            value = None  # A model/parent key never masquerades as an official collection key.
        elif value is None or key_value(task["answer_key"]) != value:
            raise FigureDoubt("сохранённый ключ отличается от исходного ключа сборника")
    if figure_proof.text_leak(task, value):
        raise FigureDoubt("в условии или вариантах раскрыт ответ")
    return value


def _save_report(conn, task_id, revision, digest, report):
    conn.execute(
        "INSERT OR REPLACE INTO figure_assessment(task_item_id,revision,input_digest,report,created_at) "
        "VALUES(?,?,?,?,?)",
        (task_id, revision, digest, json.dumps(report, ensure_ascii=False), now()),
    )


def assessment(conn, task):
    row = conn.execute(
        "SELECT input_digest,report FROM figure_assessment WHERE task_item_id=? AND revision=?",
        (task["id"], task["revision"]),
    ).fetchone()
    if row and row[0] == figure_proof.digest(task):
        return json.loads(row[1])
    return None


def _persist_file(ctx, relative, payload):
    """Immutable, durable bytes precede the DB reference; rollback leaves only an invisible orphan."""
    target = ctx.material_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise FigureDoubt("сохранённая вырезка изменилась")
        return
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".figure-stage-", delete=False
        ) as out:
            staged = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(staged, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise FigureDoubt("сохранённая вырезка изменилась") from None
    finally:
        if staged is not None:
            os.unlink(staged)  # Only our just-created, reproducible staging file.
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _publish(ctx, conn, task, prepared, report, value):
    figure = deepcopy(figure_of(task))
    author = report["author"]
    figure.update(
        regions=prepared["regions"],
        rect=prepared["regions"][0]["rect"],
        alt=author["alt"],
        teacher_note=author["overview"]
        + "\n"
        + "\n".join(d["label"] + ": " + d["text"] for d in author["descriptions"]),
        teacher_note_ack=False,
        description_source="models",
        no_answer_ack=False,
        leak=[],
        leak_ack=False,
        items_v2=True,
        confirmed=True,
        tiles_v=tiles.TILES_VERSION,
    )
    prefix = f"figures/auto-{task['id']}-r{task['revision']}-"
    figure["file"] = prefix + hashlib.sha256(prepared["common"]).hexdigest() + ".png"
    _persist_file(ctx, figure["file"], prepared["common"])
    figure["tiles"] = deepcopy(prepared["plan"]["tiles"])
    figure["tile_order"] = prepared["plan"]["order"]
    for tile, payload in zip(tiles.ordered(prepared["plan"]), prepared["parts"]):
        record = next(t for t in figure["tiles"] if t["id"] == tile["id"])
        record["file"] = prefix + hashlib.sha256(payload).hexdigest() + ".png"
        _persist_file(ctx, record["file"], payload)
    update_task(
        conn,
        task["id"],
        figure=figure,
        answer_key=key_json(value, task["type"]),
        key_source="generated" if task["key_source"] == "generated" else "official",
        key_version=task["key_version"] + int(task["answer_key"] is None),
        import_status="draft",
        unsupported_reason=None,
    )
    current = get_task(conn, task["id"])
    verified_digest = figure_proof.digest(current)
    figure["auto_verification"] = {
        "revision": current["revision"],
        "digest": verified_digest,
        "descriptions_verified": True,
        "families": report["families"],
        "models": report["models"],
    }
    update_task(conn, task["id"], figure=figure)
    chosen = sampling.reserve(conn, task["id"], task["revision"], verified_digest)
    update_task(
        conn,
        task["id"],
        import_status="draft" if chosen else "checked",
        auto_published=int(not chosen),
        auto_hold=HOLD_SAMPLE if chosen else None,
        auto_checked=int(task["key_source"] == "generated"),
    )
    save_snapshot(conn, task["id"])
    _save_report(conn, task["id"], task["revision"], verified_digest, report)
    return HOLD_SAMPLE if chosen else None


def try_publish(ctx, conn, task_id: int, state: dict) -> str | None:
    task = get_task(conn, task_id, with_import=True)
    if not _eligible(task):
        return None
    if figure_proof.verified(task):
        return task["auto_hold"]  # A sampled revision waits without new model calls or counters.
    initial = figure_proof.digest(task)
    identity = (task["revision"], task["material_id"], task["import_run_id"])
    config = figure_models.configuration(conn)
    report = {"configuration": config}
    try:
        if state.get("limit"):
            raise FigureDoubt("дневной лимит запросов к модели исчерпан")
        value = _key(ctx, conn, task)
        prepared = _prepare(ctx, conn, task)
        images = [prepared["common"], *prepared["parts"]]
        figure_models.solve(ctx.model, task, images, config, report)
        author, critic = report["author"], report["critic"]
        if figure_proof.alt_problem(task, author["alt"], value):
            raise FigureDoubt("подпись к рисунку раскрывает вариант ответа")
        if value is not None and (author["value"] != value or critic["value"] != value):
            raise FigureDoubt("решение одной из моделей расходится с ключом сборника")
        reason = None if value is not None else HOLD_KEYLESS
    except FigureDoubt as exc:
        reason = HOLD + str(exc)
        if "лимит" in reason:
            state["limit"] = True
    except Exception:  # noqa: BLE001 - an unexpected model/parser failure must fail closed.
        reason = HOLD + "не удалось завершить проверку; задание осталось вам."
    with atomic(conn):
        current = get_task(conn, task_id, with_import=True)
        if not _eligible(current):
            return HOLD_STALE
        if (
            figure_proof.digest(current) != initial
            or (current["revision"], current["material_id"], current["import_run_id"]) != identity
            or figure_models.configuration(conn) != config
        ):
            update_task(conn, task_id, auto_hold=HOLD_STALE)
            return HOLD_STALE
        if reason is None:
            try:
                if (
                    _key(ctx, conn, current) != value
                    or hashlib.sha256(prepared["source"].read_bytes()).hexdigest()
                    != prepared["source_hash"]
                ):
                    raise FigureDoubt("исходные данные изменились после проверки")
                return _publish(ctx, conn, current, prepared, report, value)
            except (FigureDoubt, OSError) as exc:
                reason = HOLD + (
                    str(exc) if isinstance(exc, FigureDoubt) else "исходный файл недоступен"
                )
        figure = figure_of(current)
        if "author" in report:
            author = report["author"]
            if not figure.get("teacher_note_ack") and all(
                isinstance(author.get(k), str) for k in ("alt", "overview")
            ):
                figure.update(
                    alt=author["alt"],
                    teacher_note=author["overview"]
                    + "\n"
                    + "\n".join(
                        str(d.get("label", "")) + ": " + str(d.get("text", ""))
                        for d in author.get("descriptions", [])
                        if isinstance(d, dict)
                    ),
                    description_source="models",
                )
                update_task(conn, task_id, figure=figure)
        update_task(conn, task_id, auto_hold=reason)
        current = get_task(conn, task_id)
        report["reason"] = reason
        _save_report(conn, task_id, current["revision"], figure_proof.digest(current), report)
        save_snapshot(conn, task_id)
        return reason


def revalidate_official(ctx, conn):
    """A conflicting/removed key file withdraws prior machine publication, even if the chosen candidate stayed equal."""
    from app.ingest.store import new_revision

    cache = {}
    for task in conn.execute(
        "SELECT * FROM task_item WHERE figure IS NOT NULL AND key_source='official'"
    ).fetchall():
        if not figure_proof.verified(task):
            continue
        try:
            value = _key(ctx, conn, task, cache)
            if value != key_value(task["answer_key"]):
                raise FigureDoubt("исходный ключ изменился")
        except FigureDoubt as exc:
            new_revision(conn, task["id"], auto_hold=HOLD + str(exc))
