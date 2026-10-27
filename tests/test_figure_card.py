"""Карточка рисунка показывает вид ребёнка; растровая вырезка проверяется глазами (SPEC-1.0 1.1, #2110)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    db,
    doc_bytes,
    figure_pdf,
    make_task,
    reply,
    task_ids,
    upload,
    write_lines,
)

NODE = shutil.which("node")
WARNING = "Текст внутри картинок не проверяется автоматически"
CELLS = [
    {"label": "А", "text": "митохондрия"},
    {"label": "Б", "text": "рибосома"},
    {"label": "В", "text": "ядро"},
    {"label": "Г", "text": "лизосома"},
]
MATCH = {
    "left": [{"label": "1", "text": "рисунок 1"}, {"label": "2", "text": "рисунок 2"}],
    "right": [
        {"label": "А", "text": "минога"},
        {"label": "Б", "text": "улитка"},
        {"label": "В", "text": "пиявка"},
    ],
}


def printed_pdf(word: str) -> bytes:
    """Страница с картинкой, в рамке которой напечатано слово."""
    doc = pymupdf.open("pdf", figure_pdf("raster"))
    write_lines(doc[0], [word], x=70, y=135, size=12)
    return doc_bytes(doc)


def start(tmp_path, task, pdf):
    model = StubModel(split=reply(task), answer=answer_by_number({"11": "Б"}))
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    assert upload(c, pdf).status_code == 202
    return c, task_ids(tmp_path)[0]


def fragment(c, tid):
    r = c.get(f"/parent/review/{tid}?fragment=1", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.text


def post_figure(c, tid, **kw):
    body = {"alt": "Части объекта обозначены цифрами", "no_answer_ack": True}
    body.update(kw)
    return c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body)


def test_reported_leak_cause_is_caught(tmp_path):
    task = make_task(11, "Рассмотрите рисунок и выберите верный ответ.", figure=True, options=CELLS)
    c, tid = start(tmp_path, task, figure_pdf("raster"))
    with c:
        card = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["card"]
        # В рамке одни картинки, текстового слоя нет: автоматическая проверка ничего не видит.
        assert card["figure_check"]["raster_only"] is True
        html = fragment(c, tid)
        assert WARNING in html and "посмотрите на вырезку глазами" in html
        # Без явного ответа на шаг 1 рисунок не подтверждается.
        refused = post_figure(c, tid, no_answer_ack=False)
        assert refused.status_code == 422 and "нет ответа" in refused.json()["error"]
        ok = post_figure(c, tid)
        assert ok.status_code == 200 and ok.json()["raster_only"] is True
        stored = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["figure"]
        assert stored["confirmed"] is True and stored["raster_only"] is True

    # Рамка с текстом варианта после ручной правки: без leak_ack отказ, правка рамки не исключение.
    c, tid = start(tmp_path / "t", task, printed_pdf("митохондрия"))
    with c:
        frame = {"rect_px": [80, 150, 400, 400], "scale": 1.5, "page": 1}
        no_ack = post_figure(c, tid, **frame)
        assert no_ack.status_code == 422 and "митохондрия" in no_ack.json()["error"]
        figure = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()["figure"]
        assert figure["confirmed"] is False
        assert post_figure(c, tid, leak_ack=True, **frame).status_code == 200
        # Подтверждённый рисунок: новая правка рамки снова упирается в ту же проверку.
        again = post_figure(c, tid, rect_px=[90, 160, 390, 390], scale=1.5, page=1)
        assert again.status_code == 422
        detail = c.get(f"/parent/tasks/{tid}", headers=PARENT).json()
        assert detail["figure"]["confirmed"] is True and detail["revision"] == 2


def counts(tmp_path):
    conn = db(tmp_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def set_regions(tmp_path, tid):
    conn = db(tmp_path)
    figure = json.loads(
        conn.execute("SELECT figure FROM task_item WHERE id = ?", (tid,)).fetchone()[0]
    )
    figure["regions"] = [
        {"page": 1, "rect": [60, 120, 260, 190]},
        {"page": 1, "rect": [60, 200, 260, 270]},
    ]
    conn.execute("UPDATE task_item SET figure = ? WHERE id = ?", (json.dumps(figure), tid))
    conn.close()


def mode(c, tid, note, ack):
    r = c.post(
        f"/parent/tasks/{tid}/figure/mode",
        headers=PARENT,
        json={"teacher_note": note, "teacher_note_ack": ack},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_card_shows_child_view_and_explains_checkboxes(tmp_path):
    task = make_task(11, "Определите, где чей рот.", type="match", figure=True, options=MATCH)
    c, tid = start(tmp_path, task, figure_pdf("raster"))
    with c:
        set_regions(tmp_path, tid)
        before = counts(tmp_path)
        html = fragment(c, tid)
        assert "Как увидит ребёнок" in html
        # общий с ребёнком фрагмент: тот же select на каждый левый элемент
        assert 'name="pair-1"' in html and 'name="pair-2"' in html
        assert "На картинках выше нет ответа?" in html
        assert "Правильный ответ" in html and "Что на рисунках" in html
        assert html.count('class="region-badge"') == 2  # номера областей на рамках страницы
        assert "Подтвердить рисунок" not in html and "teacher_note_ack" not in html
        assert "Только проверка по ключу" in html

        # режим считает общая функция: описаны все обозначения и родитель подтвердил
        assert mode(c, tid, "", False)["assist"] == "key_only"
        note = "1 — круглая присоска; 2 — тёрка"
        assert mode(c, tid, note, False)["assist"] == "key_only"  # описание без подтверждения
        on = mode(c, tid, note, True)
        assert on["assist"] == "hints" and "подсказк" in on["label"].lower()
        assert mode(c, tid, "1 — присоска", True)["assist"] == "key_only"  # описан не каждый номер

        # предпросмотр: вырезка по текущим областям; правка рамки меняет вид
        base = c.get(f"/parent/tasks/{tid}/figure-preview.png", headers=PARENT)
        assert base.status_code == 200 and base.headers["content-type"] == "image/png"
        edited = c.get(
            f"/parent/tasks/{tid}/figure-preview.png",
            params={"page": 1, "rect_px": "90,180,300,260", "scale": 1.5},
            headers=PARENT,
        )
        assert edited.status_code == 200
        one, two = pymupdf.Pixmap(base.content), pymupdf.Pixmap(edited.content)
        assert (one.width, one.height) != (two.width, two.height)
        # ни попыток, ни событий, ни запросов модели, ни файлов рисунка
        assert counts(tmp_path) == before
        assert not (tmp_path / "figures").exists() or not list((tmp_path / "figures").iterdir())

        # ребёнок: ни предпросмотра, ни рисунка неподтверждённого задания
        assert c.get(f"/parent/tasks/{tid}/figure-preview.png", headers=CHILD).status_code == 403
        assert c.get(f"/tasks/{tid}/figure.png", headers=CHILD).status_code == 404
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 404
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []

        # публикация: ребёнок и родитель получают один и тот же блок ответов
        key = c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "1-А, 2-Б"})
        assert key.status_code == 200, key.text
        assert post_figure(c, tid, teacher_note=note, teacher_note_ack=True).status_code == 200
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 200
        child = c.get(f"/tasks/{tid}", headers=CHILD)
        assert child.status_code == 200
        pick = re.compile(r'<select name="pair-1">.*?</select>', re.DOTALL)
        assert pick.search(child.text).group(0) == pick.search(html).group(0)


def test_published_match_keeps_hints_and_new_rule_needs_the_mark():
    from app.ingest.publish import assist_mode

    plain = {**MATCH, "left": [{"label": "1", "text": "первый"}, {"label": "2", "text": "второй"}]}

    def task(figure):
        return {"options": json.dumps(plain), "figure": json.dumps(figure)}

    old = {"teacher_note": "", "teacher_note_ack": True, "confirmed": True}
    # запись, опубликованная до #2110: правило режима не меняется
    assert assist_mode(task(old)) == "hints"
    # рисунок, подтверждённый после правки, отмечен items_v2: левые номера match нужно описать
    assert assist_mode(task({**old, "items_v2": True})) == "key_only"
    described = {**old, "items_v2": True, "teacher_note": "1 — а\n2 — б"}
    assert assist_mode(task(described)) == "hints"


def test_publishing_needs_the_explicit_step_one_on_the_server(tmp_path):
    task = make_task(11, "Рассмотрите рисунок и выберите верный ответ.", figure=True, options=CELLS)
    c, tid = start(tmp_path, task, figure_pdf("raster"))
    with c:
        # рисунок не подтверждён: публикация другого шага цепочки невозможна
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 422
        assert (
            c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "Б"}).status_code
            == 200
        )
        assert post_figure(c, tid, no_answer_ack=False).status_code == 422
        ok = post_figure(c, tid)
        assert ok.status_code == 200, ok.text
        # подтверждённый рисунок без записанного шага 1 (старая или чужая запись) не публикуется
        conn = db(tmp_path)
        figure = json.loads(
            conn.execute("SELECT figure FROM task_item WHERE id = ?", (tid,)).fetchone()[0]
        )
        figure["no_answer_ack"] = False
        conn.execute("UPDATE task_item SET figure = ? WHERE id = ?", (json.dumps(figure), tid))
        conn.close()
        refused = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        assert refused.status_code == 422 and "нет ответа" in refused.json()["error"]
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []


@pytest.mark.skipif(NODE is None, reason="node не установлен")
def test_the_chain_works_only_on_the_snapshot_card():
    """Ключ задерживается, «Пропустить» нажимают, карточка меняется на B: шаги идут только для A."""
    harness = Path(__file__).parent / "js" / "feed_chain_harness.js"
    r = subprocess.run(
        [NODE, str(harness), "skip_during_delayed_key"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
