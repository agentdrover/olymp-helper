"""Плитки рисунка и карточка рисунка v2 (SPEC-1.0 1.1, #2121)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.ingest import tiles
from app.ingest.publish import source_caption
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    build_app,
    db,
    doc_bytes,
    make_task,
    quad_png,
    reply,
    row,
    task_ids,
    upload,
    write_lines,
)

NODE = shutil.which("node")
ANIMALS = ["минога", "улитка", "пиявка", "муравей", "осьминог", "миксина"]
MATCH6 = {
    "left": [{"label": str(i), "text": f"рисунок {i}"} for i in range(1, 7)],
    "right": [{"label": c, "text": t} for c, t in zip("АБВГДЕ", ANIMALS, strict=True)],
}
KEY6 = "1-А, 2-Б, 3-В, 4-Г, 5-Д, 6-Е"
FRAME_PX = [80, 150, 700, 800]  # рамка, нарисованная родителем, в пикселях при масштабе 1.5
TEXT = "Определите, где чей рот у животных."
COLORS = [(250, 0, 0), (0, 200, 0), (0, 0, 250), (230, 230, 0), (0, 200, 200), (200, 0, 200)]


def put(page, rect, stream):
    page.insert_image(rect, stream=stream, keep_proportion=False)


def solid(color, w=120, h=90) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h))
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def grid_page(doc, count=6, captions=True, text_in=None, task_text=True):
    """Страница: условие и count картинок в две колонки, подпись сразу под каждой."""
    page = doc.new_page(width=600, height=800)
    if task_text:
        write_lines(page, [f"11. {TEXT}"], x=50, y=60)
    for i in range(count):
        x, y = (60, 110 + (i // 2) * 140) if i % 2 == 0 else (300, 110 + (i // 2) * 140)
        put(page, pymupdf.Rect(x, y, x + 140, y + 90), solid(COLORS[i]))
        if captions:
            write_lines(page, [f"Рис. {i + 1}"], x=x + 6, y=y + 102, size=10)
    for i, word in (text_in or {}).items():
        x, y = (60, 110 + (i // 2) * 140) if i % 2 == 0 else (300, 110 + (i // 2) * 140)
        write_lines(page, [word], x=x + 10, y=y + 40, size=12)
    return page


def grid_pdf(**kw) -> bytes:
    doc = pymupdf.open()
    grid_page(doc, **kw)
    return doc_bytes(doc)


def reopened(pdf: bytes):
    return pymupdf.open("pdf", pdf)


def whole(page) -> list[float]:
    return [0, 90, 600, 780]


# ------------------------------------------------------------------ плитки по изображениям


def test_images_inside_frame_become_numbered_tiles(tmp_path):
    # шесть изображений -> шесть плиток в порядке чтения (строками слева направо)
    doc = reopened(grid_pdf())
    found = tiles.suggest_page_tiles(doc[0], 1, whole(doc[0]))
    assert len(found) == 6 and len({t["id"] for t in found}) == 6
    assert [t["rect"][0] for t in found] == [60, 300] * 3
    assert [t["rect"][1] for t in found][::2] == sorted(t["rect"][1] for t in found)[::2]
    # плитка расширена до подписи под картинкой, но не до подписи соседа
    first = found[0]["rect"]
    assert first[3] > 200 and first[3] < 250  # подпись «Рис. 1» внутри плитки

    # шесть изображений в столбик для match: плитки подряд, привязка по меткам
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i in range(6):
        put(page, pymupdf.Rect(60, 40 + i * 120, 200, 130 + i * 120), solid(COLORS[i]))
    column = reopened(doc_bytes(doc))
    plan = tiles.plan_tiles(column, {}, [{"page": 1, "rect": [0, 0, 600, 780]}])
    tiles.bind_defaults(plan, [str(i) for i in range(1, 7)])
    assert [t["label"] for t in tiles.ordered(plan)] == ["1", "2", "3", "4", "5", "6"]

    # одно изображение и полосы одной картинки - общая вырезка, плиток нет
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i in range(3):  # одна картинка, нарезанная на смежные полосы
        put(page, pymupdf.Rect(60, 100 + i * 30, 260, 130 + i * 30), solid(COLORS[i]))
    only_strips = reopened(doc_bytes(doc))
    assert tiles.suggest_page_tiles(only_strips[0], 1, whole(only_strips[0])) == []
    # полосы + отдельная картинка: полосы - одна плитка
    put(page, pymupdf.Rect(60, 300, 260, 390), solid(COLORS[5]))
    mixed = reopened(doc_bytes(doc))
    pair = tiles.suggest_page_tiles(mixed[0], 1, whole(mixed[0]))
    assert len(pair) == 2 and pair[0]["rect"][1] == 100 and pair[0]["rect"][3] == 190
    # смежные, но не образующие прямоугольник - неоднозначно: общая вырезка
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    put(page, pymupdf.Rect(60, 100, 260, 200), solid(COLORS[0]))
    put(page, pymupdf.Rect(260, 150, 360, 250), solid(COLORS[1]))
    put(page, pymupdf.Rect(60, 400, 260, 500), solid(COLORS[2]))
    odd = reopened(doc_bytes(doc))
    assert tiles.suggest_page_tiles(odd[0], 1, whole(odd[0])) == []

    # маска относится к основному изображению: ни своей плитки, ни второй копии
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    mask = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 120, 90))
    mask.set_rect(mask.irect, (200,))
    page.insert_image(
        pymupdf.Rect(60, 100, 200, 190),
        stream=solid(COLORS[0]),
        mask=mask.tobytes("png"),
        keep_proportion=False,
    )
    put(page, pymupdf.Rect(60, 100, 200, 190), solid(COLORS[0]))  # повторная отрисовка
    put(page, pymupdf.Rect(60, 300, 200, 390), solid(COLORS[1]))
    masked = reopened(doc_bytes(doc))
    assert len(tiles.suggest_page_tiles(masked[0], 1, whole(masked[0]))) == 2

    # один объект на двух местах страницы - две плитки
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for y in (100, 300):
        put(page, pymupdf.Rect(60, y, 200, y + 90), quad_png())
    twice = reopened(doc_bytes(doc))
    placed = tiles.suggest_page_tiles(twice[0], 1, whole(twice[0]))
    assert len(placed) == 2 and placed[0]["id"] != placed[1]["id"]

    # подпись вне bbox изображения попадает в плитку своей ячейки
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    put(page, pymupdf.Rect(60, 100, 200, 190), solid(COLORS[0]))
    put(page, pymupdf.Rect(300, 100, 440, 190), solid(COLORS[1]))
    write_lines(page, ["Левая подпись"], x=60, y=205, size=10)
    write_lines(page, ["Правая подпись"], x=300, y=205, size=10)
    capt = reopened(doc_bytes(doc))
    left, right = tiles.suggest_page_tiles(capt[0], 1, whole(capt[0]))
    assert left["rect"][3] > 205 and left["rect"][2] < 300
    assert right["rect"][3] > 205 and right["rect"][0] >= 300 - 1

    # старая запись читается как прежняя общая картинка
    old = {"page": 1, "rect": [60, 120, 260, 190], "confirmed": True, "file": "figures/1-r2.png"}
    assert tiles.stored_tiles(old) is None and tiles.stored_view(old) == []
    assert tiles.figure_files(old) == ["figures/1-r2.png"]


def test_frame_edit_recomputes_only_its_page_and_keeps_labels():
    doc = pymupdf.open()
    grid_page(doc, count=4, task_text=False)
    grid_page(doc, count=2, task_text=False)
    pdf = reopened(doc_bytes(doc))
    regions = [
        {"page": 1, "rect": [0, 90, 600, 780]},
        {"page": 2, "rect": [0, 90, 600, 780]},
    ]
    first = tiles.plan_tiles(pdf, {}, regions)
    assert [t["page"] for t in first["tiles"]] == [1] * 4 + [2] * 2
    saved = {
        "tiles_v": 1,
        "tiles": [{**t, "label": str(n)} for n, t in enumerate(first["tiles"], 1)],
        "tile_order": list(reversed(first["order"])),
    }
    saved["tiles"][0]["rect"] = [1.0, 2.0, 3.0, 4.0]  # метка сохранённого пересчёт не трогает
    kept = tiles.plan_tiles(pdf, saved, regions)
    assert kept["tiles"][0]["rect"] == [1.0, 2.0, 3.0, 4.0]  # страницу не правили - как сохранено
    assert kept["order"] == saved["tile_order"]
    # рамку страницы 2 сузили до одной картинки: пересчитана только она, страница 1 осталась
    narrow = [regions[0], {"page": 2, "rect": [0, 90, 280, 250]}]
    after = tiles.plan_tiles(pdf, saved, narrow, edited_page=2)
    assert [t["page"] for t in after["tiles"]].count(1) == 4
    assert after["tiles"][0]["rect"] == [1.0, 2.0, 3.0, 4.0]
    # на странице 2 одна картинка: плиток нет, но область не пропадает - она плитка целиком
    page2 = [t for t in after["tiles"] if t["page"] == 2]
    assert len(page2) == 1 and page2[0]["rect"] == [0, 90, 280, 250]


# ------------------------------------------------------------------ карточка v2 целиком


def boot(tmp_path, pdf, task=None, **extra):
    task = task or make_task(11, TEXT, type="match", figure=True, options=MATCH6)
    model = StubModel(split=reply(task))
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    assert upload(c, pdf, **extra).status_code == 202
    return c, task_ids(tmp_path)[0]


def card_of(c, tid):
    r = c.get(f"/parent/tasks/{tid}", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.json()["card"]


def fragment(c, tid):
    r = c.get(f"/parent/review/{tid}?fragment=1", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.text


def figure_body(card, **kw):
    body = {
        "alt": "Шесть ртов крупным планом",
        "no_answer_ack": True,
        "rect_px": FRAME_PX,
        "scale": 1.5,
        "page": 1,
        "tiles": [{"id": t["id"], "label": t["label"]} for t in card["tiles"]],
    }
    body.update(kw)
    return body


def set_key(c, tid, key=KEY6):
    r = c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": key})
    assert r.status_code == 200, r.text


def stored_figure(tmp_path, tid):
    return json.loads(row(tmp_path, tid)["figure"])


def digest_for(c, tid, **params):
    url = f"/parent/tasks/{tid}/figure-tiles"
    found = re.search(
        r'data-leak-digest="(\w+)"',
        c.get(url, params=tiles_params(**params), headers=PARENT).text,
    )
    assert found
    return found.group(1)


def test_card_v2_layout_and_readiness(tmp_path):
    pdf = grid_pdf(captions=False, text_in={2: "лишайник"})
    c, tid = boot(tmp_path / "a", pdf)
    with c:
        html = fragment(c, tid)
        # раскладка: верхняя полоса и три колонки
        strip = html[html.index('class="fc-bar"') : html.index('class="fc-cols"')]
        order = ["Исключить", "Поправить текст", "Пропустить", "Подтвердить и опубликовать"]
        assert [strip.index(w) for w in order] == sorted(strip.index(w) for w in order)
        assert 'class="fc-counter"' in html and "Enter" in strip
        assert html.count('class="fc-col fc-pages"') == 1
        assert html.count('class="fc-col fc-child"') == 1
        assert html.count('class="fc-col fc-check"') == 1
        css = Path("app/static/app.css").read_text()
        assert (
            "@media (min-width: 1280px)" in css
            and ".fc-cols { grid-template-columns: minmax(300px" in css
        )
        # ввод ответа под каждой плиткой вместо отдельного списка шага 2
        assert html.count("data-key-left=") == 6 and html.count('class="fc-tile"') == 6
        assert "Правильный ответ</h3>" not in html and 'class="steps"' not in html
        assert "На картинках нет ответа?" in html and "Как увидит ребёнок" in html
        assert "Описать рисунки для подсказок · 0 из 6" in html and "Описание верное" in html
        assert html.count('data-note="') == 6
        assert 'id="why-slot"' in html  # место под «почему так» есть только у родителя

        # смешанные плитки: предупреждение у каждой плитки без текстового слоя
        card = card_of(c, tid)
        raster = [t["raster_only"] for t in card["tiles"]]
        assert raster == [True, True, False, True, True, True]
        assert html.count('class="tile-warning"') == 5
        assert [t["n"] for t in card["tiles"]] == [1, 2, 3, 4, 5, 6]
        assert [t["label"] for t in card["tiles"]] == ["1", "2", "3", "4", "5", "6"]
        assert card["tiles"][2]["leak"] == [] and card["tiles"][2]["raster_only"] is False

        # #2110 на плитках: без ответа на шаг 1 - отказ
        set_key(c, tid)
        no_ack = c.post(
            f"/parent/tasks/{tid}/figure",
            headers=PARENT,
            json=figure_body(card, no_answer_ack=False),
        )
        assert no_ack.status_code == 422 and "нет ответа" in no_ack.json()["error"]
        # слово варианта напечатано в плитке 3 (страница) - утечка по этой плитке
        leaky = pdf_with_option_word(tmp_path / "leak")
        c2, tid2 = boot(tmp_path / "b", leaky)
        with c2:
            card2 = card_of(c2, tid2)
            leak_tile = [t for t in card2["tiles"] if t["leak"]]
            assert len(leak_tile) == 1 and leak_tile[0]["n"] == 3
            set_key(c2, tid2)
            refused = c2.post(
                f"/parent/tasks/{tid2}/figure", headers=PARENT, json=figure_body(card2)
            )
            assert refused.status_code == 422 and "миксина" in refused.json()["error"]
            assert stored_figure(tmp_path / "b", tid2)["confirmed"] is False
            ok = c2.post(
                f"/parent/tasks/{tid2}/figure",
                headers=PARENT,
                json=figure_body(card2, leak_ack=True, leak_digest=digest_for(c2, tid2)),
            )
            assert ok.status_code == 200, ok.text
            # правка рамки после подтверждения снова упирается в ту же проверку
            again = c2.post(
                f"/parent/tasks/{tid2}/figure",
                headers=PARENT,
                json=figure_body(card2, rect_px=[90, 160, 700, 790]),
            )
            assert again.status_code == 422

        # предпросмотр и подтверждение: одни и те же границы, состав и порядок
        listing = c.get(
            f"/parent/tasks/{tid}/figure-tiles",
            params={"page": 1, "rect_px": ",".join(map(str, FRAME_PX)), "scale": 1.5},
            headers=PARENT,
        )
        assert listing.status_code == 200
        shown = re.findall(r'data-tile-id="(\w+)"', listing.text)
        done = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card))
        assert done.status_code == 200, done.text
        figure = stored_figure(tmp_path / "a", tid)
        assert figure["tile_order"] == shown and figure["tiles_v"] == 1
        assert [t["id"] for t in figure["tiles"]] == shown
        for tile in figure["tiles"]:
            assert tile["file"] and (tmp_path / "a" / tile["file"]).exists()
        preview = c.get(
            f"/parent/tasks/{tid}/tile-preview/{shown[0]}.png",
            params={"page": 1, "rect_px": ",".join(map(str, FRAME_PX)), "scale": 1.5},
            headers=PARENT,
        )
        saved = pymupdf.Pixmap((tmp_path / "a" / figure["tiles"][0]["file"]).read_bytes())
        assert pymupdf.Pixmap(preview.content).width == saved.width

        # ребёнок: ключ и редактор не передаются, плитки отдаются после публикации
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        page = c.get(f"/tasks/{tid}", headers=CHILD).text
        for secret in ("data-key-left", "key-fields", "why-slot", "data-note", "data-tile-label"):
            assert secret not in page
        assert page.count("data-tile-img") == 6 and "tile-warning" not in page
        api = c.get(f"/api/tasks/{tid}", headers=CHILD).json()
        assert [t["label"] for t in api["figure"]["tiles"]] == ["1", "2", "3", "4", "5", "6"]
        assert "answer_key" not in json.dumps(api) and "teacher_note" not in json.dumps(api)
        first = figure["tiles"][0]["id"]
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=CHILD).status_code == 200
        assert c.get(f"/tasks/{tid}/figure/nonexistent.png", headers=CHILD).status_code == 404


def pdf_with_option_word(_tmp):
    return grid_pdf(captions=False, text_in={2: "миксина"})


def test_tile_binding_follows_labels_and_rebinding_needs_a_new_revision(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        first = stored_figure(tmp_path, tid)
        assert [t["label"] for t in first["tiles"]] == ["1", "2", "3", "4", "5", "6"]
        key_before = row(tmp_path, tid)["answer_key"]
        # перестановка показа: ключ и описание остаются за метками, привязка прежняя
        reordered = list(reversed(card["tiles"]))
        body = figure_body(
            card,
            tiles=[{"id": t["id"], "label": t["label"]} for t in reordered],
            teacher_note="1 — а\n2 — б",
        )
        assert c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body).status_code == 200
        second = stored_figure(tmp_path, tid)
        assert second["tile_order"] == [t["id"] for t in reordered]
        assert {t["id"]: t["label"] for t in second["tiles"]} == {
            t["id"]: t["label"] for t in first["tiles"]
        }
        assert row(tmp_path, tid)["answer_key"] == key_before
        assert second["teacher_note"] == "1 — а\n2 — б"
        # смена привязки: новая ревизия, снова нужно подтвердить
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        revision = row(tmp_path, tid)["revision"]
        swapped = [dict(t) for t in body["tiles"]]
        swapped[0]["label"], swapped[1]["label"] = swapped[1]["label"], swapped[0]["label"]
        done = c.post(
            f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "tiles": swapped}
        )
        assert done.status_code == 200
        assert row(tmp_path, tid)["revision"] == revision + 1
        assert row(tmp_path, tid)["import_status"] == "draft"
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []
        # один номер на двух плитках и чужой номер - отказ
        dup = [dict(t) for t in body["tiles"]]
        dup[1]["label"] = dup[0]["label"]
        bad = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "tiles": dup})
        assert bad.status_code == 422
        alien = [dict(t) for t in body["tiles"]]
        alien[0]["label"] = "9"
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "tiles": alien}
            ).status_code
            == 422
        )
        # состав плиток изменился после предпросмотра - отказ, а не молчаливая замена
        stale = [{"id": "tdeadbeef", "label": None}, *body["tiles"][1:]]
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "tiles": stale}
            ).status_code
            == 409
        )


def test_tile_count_mismatch_warns_and_keeps_full_input(tmp_path):
    c, tid = boot(tmp_path, grid_pdf(count=4))  # 4 плитки на 6 левых элементов
    with c:
        card = card_of(c, tid)
        assert len(card["tiles"]) == 4 and card["tile_warning"]
        html = fragment(c, tid)
        assert "tile-count-warning" in html
        # полный ввод по всем левым элементам; под плитками ввода нет
        assert html.count("data-key-left=") == 6
        assert 'class="fc-tile-key"' not in html
        # предупреждение публикацию не блокирует
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )


def test_old_records_and_cleanup(tmp_path):
    # одно изображение: общая вырезка, как раньше
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    write_lines(page, [f"11. {TEXT}"], x=50, y=60)
    put(page, pymupdf.Rect(60, 110, 260, 250), quad_png())
    c, tid = boot(tmp_path, doc_bytes(doc))
    with c:
        card = card_of(c, tid)
        assert card["tiles"] == []
        assert "tiles" not in stored_figure(tmp_path, tid)  # предложение не сохраняется
        set_key(c, tid)
        body = figure_body(card)
        body.pop("tiles")
        body.pop("rect_px")
        assert c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body).status_code == 200
        figure = stored_figure(tmp_path, tid)
        assert figure["tiles"] == [] and figure["file"]
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        api = c.get(f"/api/tasks/{tid}", headers=CHILD).json()
        assert api["figure"]["tiles"] == []  # плиток нет: ребёнок видит общую вырезку

    # «Разобрать заново» и удаление материала убирают PNG всех плиток
    c, tid = boot(tmp_path / "t", grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        figure = stored_figure(tmp_path / "t", tid)
        files = [tmp_path / "t" / t["file"] for t in figure["tiles"]]
        assert len(files) == 6 and all(f.exists() for f in files)
        common = tmp_path / "t" / figure["file"]
        # родитель подтвердил рисунок, но задание не опубликовано: удаление материала уносит все PNG
        conn = db(tmp_path / "t")
        material = conn.execute("SELECT id FROM material").fetchone()[0]
        conn.execute(
            "UPDATE task_item SET import_status = 'unsupported', revision = 1, key_version = 1, "
            "key_source = NULL, answer_key = NULL, "
            "figure = json_set(figure, '$.confirmed', 0) WHERE id = ?",
            (tid,),
        )
        conn.close()
        r = c.post(
            f"/parent/materials/{material}/reparse",
            headers={**PARENT, "Accept": "application/json"},
        )
        assert r.status_code == 202, r.text
        assert not any(f.exists() for f in files) and not common.exists()

    c, tid = boot(tmp_path / "d", grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        figure = stored_figure(tmp_path / "d", tid)
        files = [tmp_path / "d" / t["file"] for t in figure["tiles"]]
        assert all(f.exists() for f in files)
        conn = db(tmp_path / "d")
        material = conn.execute("SELECT id FROM material").fetchone()[0]
        conn.close()
        gone = c.post(
            f"/parent/materials/{material}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert gone.status_code == 303, gone.text
        assert not any(f.exists() for f in files)
        # повторное подтверждение рамки убирает PNG прежних плиток, которых больше нет
    c, tid = boot(tmp_path / "e", grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        first = [tmp_path / "e" / t["file"] for t in stored_figure(tmp_path / "e", tid)["tiles"]]
        again = figure_body(card)
        assert c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=again).status_code == 200
        assert not any(f.exists() for f in first)
        second = stored_figure(tmp_path / "e", tid)["tiles"]
        assert len(second) == 6 and all((tmp_path / "e" / t["file"]).exists() for t in second)


def test_other_types_get_one_answer_and_key_change_rechecks_every_tile(tmp_path):
    task = make_task(11, TEXT, type="numeric", figure=True, options=None)
    c, tid = boot(tmp_path, grid_pdf(captions=False, text_in={2: "42"}), task=task)
    with c:
        card = card_of(c, tid)
        assert len(card["tiles"]) == 6 and card["tile_warning"] is None
        html = fragment(c, tid)
        # не match: привязки к номерам нет, один ответ на задание
        assert html.count('id="key_text"') == 1 and "data-key-left" not in html
        assert "data-tile-bind" not in html
        assert "tiles" not in stored_figure(tmp_path, tid)  # открытие карточки плитки не сохраняет
        set_key(c, tid, "7")
        body = figure_body(card)
        assert c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body).status_code == 200
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        assert stored_figure(tmp_path, tid)["confirmed"] is True
        # ключ стал равен тексту, напечатанному в плитке 3: подтверждение снимается
        set_key(c, tid, "42")
        figure = stored_figure(tmp_path, tid)
        assert figure["confirmed"] is False and "42" in figure["leak"]
        assert row(tmp_path, tid)["import_status"] == "unsupported"
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []


def test_old_published_record_stays_a_common_picture(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        conn = db(tmp_path)  # запись вида до плиток
        conn.execute(
            "UPDATE task_item SET figure = json_remove(figure, '$.tiles', '$.tiles_v', "
            "'$.tile_order') WHERE id = ?",
            (tid,),
        )
        conn.close()
        api = c.get(f"/api/tasks/{tid}", headers=CHILD).json()
        assert api["figure"]["tiles"] == []
        assert c.get(f"/tasks/{tid}/figure.png", headers=CHILD).status_code == 200
        first = card["tiles"][0]["id"]
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=CHILD).status_code == 404
        page = c.get(f"/tasks/{tid}", headers=CHILD).text
        assert "data-tile-img" not in page and "data-figure-img" in page
        assert (
            stored_figure(tmp_path, tid).get("tiles_v") is None
        )  # опубликованное не преобразуется


def test_tile_route_keeps_the_publication_checks(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        first = stored_figure(tmp_path, tid)["tiles"][0]["id"]
        # подтверждён рисунок, но задание не опубликовано: ребёнку плитки нет, родителю есть
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=CHILD).status_code == 404
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=PARENT).status_code == 200
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=CHILD).status_code == 200
        # исключённое задание перестаёт отдавать плитки
        assert c.post(f"/parent/tasks/{tid}/exclude", headers=PARENT).status_code == 200
        assert c.get(f"/tasks/{tid}/figure/{first}.png", headers=CHILD).status_code == 404
        # предпросмотр плиток - только родителю
        assert (
            c.get(f"/parent/tasks/{tid}/tile-preview/{first}.png", headers=CHILD).status_code == 403
        )
        assert c.get(f"/parent/tasks/{tid}/figure-tiles", headers=CHILD).status_code == 403


def test_tile_ids_do_not_collide_and_regions_never_vanish(tmp_path):
    # пара мест, у которой короткий хеш совпадал
    one = tiles.tile_id(1, pymupdf.Rect(79, 391, 191, 440))
    two = tiles.tile_id(1, pymupdf.Rect(263, 45, 304, 76))
    assert one != two and len(one) > 40

    # страница 1: два изображения, страница 2: одно. Ребёнок видит все три.
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    write_lines(page, [f"11. {TEXT}"], x=50, y=60)
    put(page, pymupdf.Rect(60, 110, 200, 200), solid(COLORS[0]))
    put(page, pymupdf.Rect(300, 110, 440, 200), solid(COLORS[1]))
    page2 = doc.new_page(width=600, height=800)
    write_lines(page2, ["Продолжение рисунка на второй странице."], x=50, y=60)
    put(page2, pymupdf.Rect(60, 110, 200, 200), solid(COLORS[2]))
    task = make_task(11, TEXT, figure=True, pages=(1, 2), options=None, type="numeric")
    c, tid = boot(tmp_path, doc_bytes(doc), task=task)
    with c:
        card = card_of(c, tid)
        assert [t["page"] for t in card["tiles"]] == [1, 1, 2]
        set_key(c, tid, "7")
        body = figure_body(card)
        body.pop("rect_px")
        assert c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body).status_code == 200
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        shown = c.get(f"/tasks/{tid}", headers=CHILD).text
        assert shown.count("data-tile-img") == 3
        api = c.get(f"/api/tasks/{tid}", headers=CHILD).json()
        assert len(api["figure"]["tiles"]) == 3


def test_drawn_frame_without_stored_regions_is_previewed_and_checked(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        conn = db(tmp_path)
        conn.execute(
            "UPDATE task_item SET figure = json_set(figure, '$.regions', json('[]'), '$.rect', NULL)"
            " WHERE id = ?",
            (tid,),
        )
        conn.close()
        params = {"page": 1, "rect_px": ",".join(map(str, FRAME_PX)), "scale": 1.5}
        listing = c.get(f"/parent/tasks/{tid}/figure-tiles", params=params, headers=PARENT)
        assert listing.status_code == 200
        ids = re.findall(r'data-tile-id="(\w+)"', listing.text)
        assert len(ids) == 6 and "data-figure-img" not in listing.text
        set_key(c, tid)
        card = {"tiles": [{"id": i, "label": str(n)} for n, i in enumerate(ids, 1)]}
        body = {
            "alt": "Шесть ртов",
            "no_answer_ack": True,
            "rect_px": FRAME_PX,
            "scale": 1.5,
            "page": 1,
        }
        # клиент плиток не присылал: плитки, которых родитель не видел, не сохраняются
        silent = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body)
        assert silent.status_code == 200 and stored_figure(tmp_path, tid)["tiles"] == []
        empty = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "tiles": []})
        assert empty.status_code == 409
        other = {**card, "tiles": card["tiles"][:5]}
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, **other}
            ).status_code
            == 409
        )
        ok = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, **card})
        assert ok.status_code == 200, ok.text
        assert [t["id"] for t in stored_figure(tmp_path, tid)["tiles"]] == ids


def test_failed_reconfirmation_cleans_the_png_of_removed_tiles(tmp_path):
    doc = pymupdf.open()
    page = grid_page(doc, captions=False)
    write_lines(page, ["5 мелко"], x=320, y=410, size=2)  # подпись в плитке 6: нечитаема
    c, tid = boot(tmp_path, doc_bytes(doc))
    with c:
        set_key(c, tid)
        rows12 = [75, 150, 675, 525]  # плитки 1-4
        card = {"tiles": []}
        listing = c.get(
            f"/parent/tasks/{tid}/figure-tiles",
            params={"page": 1, "rect_px": ",".join(map(str, rows12)), "scale": 1.5},
            headers=PARENT,
        )
        ids = re.findall(r'data-tile-id="(\w+)"', listing.text)
        assert len(ids) == 4
        card["tiles"] = [{"id": i, "label": str(n)} for n, i in enumerate(ids, 1)]
        first = c.post(
            f"/parent/tasks/{tid}/figure",
            headers=PARENT,
            json=figure_body(card, rect_px=rows12),
        )
        assert first.status_code == 200, first.text
        old = [tmp_path / t["file"] for t in stored_figure(tmp_path, tid)["tiles"]]
        assert len(old) == 4 and all(f.exists() for f in old)
        rows23 = [75, 360, 675, 735]  # плитки 3-6: плитка 6 с мелкой подписью
        again = c.get(
            f"/parent/tasks/{tid}/figure-tiles",
            params={"page": 1, "rect_px": ",".join(map(str, rows23)), "scale": 1.5},
            headers=PARENT,
        )
        ids2 = re.findall(r'data-tile-id="(\w+)"', again.text)
        assert set(ids2) != set(ids) and len(ids2) == 4
        second = c.post(
            f"/parent/tasks/{tid}/figure",
            headers=PARENT,
            json=figure_body(
                {"tiles": [{"id": i, "label": None} for i in ids2]}, rect_px=rows23, tiles=None
            )
            | {"tiles": [{"id": i, "label": None} for i in ids2]},
        )
        assert second.status_code == 200 and second.json()["status"] == "unsupported"
        figure = stored_figure(tmp_path, tid)
        kept = {tmp_path / f for f in tiles.figure_files(figure)}
        leftovers = {f for f in (tmp_path / "figures").iterdir()} - kept
        assert leftovers == set()


# ------------------------------------------------------------------------ подпись источника


def test_source_caption_uses_year_or_file_name():
    with_year = {"year": 2022, "original_name": "сборник.pdf"}
    assert source_caption(with_year, 3) == "сборник 2022/23, стр. 3"
    no_year = {"year": None, "original_name": "Сеченов отбор.pdf"}
    assert source_caption(no_year, 3) == "Сеченов отбор, стр. 3"
    nameless = {"year": None, "original_name": None}
    assert source_caption(nameless, 3) == "Сборник, стр. 3"
    assert source_caption(nameless, None) == "Сборник"
    assert "sha" not in source_caption(nameless, 3).lower()


def test_source_caption_is_the_same_on_preview_and_child_routes(tmp_path):
    c, tid = boot(tmp_path, grid_pdf(), year="")
    with c:
        card = card_of(c, tid)
        assert card["child"]["source"] == "f, стр. 1"
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        assert c.get(f"/api/tasks/{tid}", headers=CHILD).json()["source"] == "f, стр. 1"


def tiles_params(**extra):
    return {"page": 1, "rect_px": ",".join(map(str, FRAME_PX)), "scale": 1.5, **extra}


def test_partial_match_key_is_refused_and_the_card_demands_every_left(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        card = card_of(c, tid)
        html = fragment(c, tid)
        assert 'data-lefts="1,2,3,4,5,6"' in html and 'data-bound="1"' in html
        set_key(c, tid, "1-А, 2-Б, 3-В, 4-Г, 5-Д")  # шестой рисунок без ответа
        refused = c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card))
        assert refused.status_code == 422 and "каждого рисунка" in refused.json()["error"]
        assert stored_figure(tmp_path, tid)["confirmed"] is False
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        # ключ обрезали уже после подтверждения рисунка: публикация тоже отказывает
        set_key(c, tid, "1-А, 2-Б, 3-В, 4-Г, 5-Д")
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 422


def digest_of(text):
    found = re.search(r'data-leak-digest="(\w+)"', text)
    assert found, text
    return found.group(1)


def test_leak_ack_is_bound_to_the_frame_and_tiles_it_was_given_for(tmp_path):
    c, tid = boot(tmp_path, pdf_with_option_word(tmp_path))
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        url = f"/parent/tasks/{tid}/figure-tiles"
        now = digest_of(c.get(url, params=tiles_params(), headers=PARENT).text)
        other = digest_of(
            c.get(url, params=tiles_params(rect_px="90,160,700,790"), headers=PARENT).text
        )
        assert now != other
        body = figure_body(card, leak_ack=True)
        stale = c.post(
            f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "leak_digest": other}
        )
        assert stale.status_code == 422 and "миксина" in stale.json()["error"]
        fresh = c.post(
            f"/parent/tasks/{tid}/figure", headers=PARENT, json={**body, "leak_digest": now}
        )
        assert fresh.status_code == 200, fresh.text
        assert stored_figure(tmp_path, tid)["leak_ack"] is True


def test_leak_ack_without_a_digest_is_ignored(tmp_path):
    c, tid = boot(tmp_path, pdf_with_option_word(tmp_path))
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        bare = c.post(
            f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card, leak_ack=True)
        )
        assert bare.status_code == 422 and "миксина" in bare.json()["error"]
        assert bare.json()["leak_digest"] == digest_for(c, tid)  # отказ сам называет слепок
        assert stored_figure(tmp_path, tid)["confirmed"] is False


def test_empty_tile_state_still_binds_tiles_by_default(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        url = f"/parent/tasks/{tid}/figure-tiles"
        none = c.get(url, params=tiles_params(), headers=PARENT).text
        empty = c.get(url, params=tiles_params(state=""), headers=PARENT).text
        assert none.count("data-key-left=") == 6 == empty.count("data-key-left=")
        assert re.findall(r'data-label="(\w*)"', empty) == ["1", "2", "3", "4", "5", "6"]


def test_child_tiles_open_in_the_large_view(tmp_path):
    c, tid = boot(tmp_path, grid_pdf())
    with c:
        card = card_of(c, tid)
        set_key(c, tid)
        assert (
            c.post(
                f"/parent/tasks/{tid}/figure", headers=PARENT, json=figure_body(card)
            ).status_code
            == 200
        )
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        page = c.get(f"/tasks/{tid}", headers=CHILD).text
        assert page.count("data-zoom-src=") == 6 and page.count('<dialog class="zoom"') == 1
        assert "data-close-zoom" in page
        # у родителя в предпросмотре окна нет, ключа у ребёнка тоже
        assert 'class="zoom"' not in fragment(c, tid) and "data-key-left" not in page
    assert "data-zoom-src" in Path("app/static/tutor.js").read_text()


# ------------------------------------------------------------------------------- JS


@pytest.mark.skipif(NODE is None, reason="node не установлен")
@pytest.mark.parametrize(
    "scenario",
    [
        "ready_state_gates_button_and_enter",
        "chain_carries_tiles",
        "tile_move_changes_order_only",
        "unbound_tile_blocks_publish",
        "frame_change_clears_leak_ack",
        "leak_ack_digest_is_sent",
        "stale_tiles_response_is_dropped",
        "reorder_keeps_the_frame_update",
        "reorder_during_pending_text_resends",
        "publish_blocked_until_tiles_fresh",
        "tiles_error_blocks_until_next_change",
    ],
)
def test_feed_tile_scenarios(scenario):
    harness = Path(__file__).parent / "js" / "feed_chain_harness.js"
    r = subprocess.run(
        [NODE, str(harness), scenario], check=False, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
