import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from app.ingest.prompt import build_task_request
from app.ingest.render import CACHE_LIMIT_BYTES, PageCache
from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    db,
    figure_pdf,
    key_pdf,
    make_task,
    reply,
    row,
    task_ids,
    upload,
)

DIGIT_OPTS = [
    {"label": "А", "text": "1, 2"},
    {"label": "Б", "text": "3, 4"},
    {"label": "В", "text": "5, 6"},
    {"label": "Г", "text": "1, 6"},
]
CELL_OPTS = [
    {"label": "А", "text": "митохондрия"},
    {"label": "Б", "text": "рибосома"},
    {"label": "В", "text": "ядро"},
    {"label": "Г", "text": "лизосома"},
]
ACK = {"no_answer_ack": True, "teacher_note_ack": True}


def fig_task(number=11, options=DIGIT_OPTS):
    return make_task(
        number, "Рассмотрите рисунок и выберите верный ответ.", figure=True, options=options
    )


@contextmanager
def figure_client(tmp_path, kind, tasks=None, **pdf_kw):
    model = StubModel(
        split=reply(*(tasks or [fig_task()])), answer=answer_by_number({"11": "Б", "12": "Б"})
    )
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        resp = upload(c, figure_pdf(kind, **pdf_kw))
        assert resp.status_code == 202, resp.text
        c.model = model
        c.material_id = resp.json()["material_id"]
        yield c


def detail(c, tid):
    r = c.get(f"/parent/tasks/{tid}", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.json()


def near(rect, expected, tol=1.5):
    return rect is not None and all(abs(a - b) <= tol for a, b in zip(rect, expected, strict=True))


def confirm_figure(c, tid, **kw):
    body = {"alt": "Рисунок: части объекта обозначены цифрами", "teacher_note": "1 2 3 4 5 6"}
    body.update(ACK)
    body.update(kw)
    if "rect_px" in kw:
        body.setdefault("page", 1)  # a drawn frame carries the page it was drawn on
    return c.post(f"/parent/tasks/{tid}/figure", headers=PARENT, json=body)


def nonwhite_bbox(pix):
    """Independent oracle: where the picture really is in the rendered page (pixels)."""
    w, h, stride, s = pix.width, pix.height, pix.stride, pix.samples
    n = pix.n
    white = b"\xff" * stride
    rows = [y for y in range(h) if s[y * stride : (y + 1) * stride] != white]
    y0, y1 = rows[0], rows[-1] + 1
    xs = [
        x
        for y in (rows[0], rows[len(rows) // 2], rows[-1])
        for x in range(w)
        if s[y * stride + x * n : y * stride + x * n + n] != b"\xff" * n
    ]
    return (min(xs), y0, max(xs) + 1, y1)


def page_pix(pdf, scale=2.0):
    doc = pymupdf.open("pdf", pdf)
    return doc[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale))


def pixel(pix, x, y):
    return tuple(pix.pixel(int(x), int(y)))[:3]


def assert_png_is_region(png_bytes, full_pix, bbox):
    png = pymupdf.Pixmap(png_bytes)
    x0, y0, x1, y1 = bbox
    assert abs(png.width - (x1 - x0)) <= 3 and abs(png.height - (y1 - y0)) <= 3
    for dx, dy in ((6, 6), (-7, 6), (6, -7), (-7, -7)):
        px = (6 if dx > 0 else png.width + dx, 6 if dy > 0 else png.height + dy)
        fx = x0 + 6 if dx > 0 else x1 + dx
        fy = y0 + 6 if dy > 0 else y1 + dy
        assert pixel(png, *px) == pixel(full_pix, fx, fy), (dx, dy)


def figure_png(c, tid):
    r = c.get(f"/tasks/{tid}/figure.png", headers=PARENT)
    assert r.status_code == 200, r.text
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    return r.content


# --------------------------------------------------------------------- AC-9


def test_ac9(tmp_path):
    with figure_client(tmp_path, "raster") as c:
        tid = task_ids(tmp_path)[0]
        d = detail(c, tid)
        assert d["status"] == "unsupported" and d["card"]["kind"] == "needs_figure"
        fig = d["figure"]
        assert fig["suggested"] is True and fig["confirmed"] is False
        assert near(fig["rect"], (60, 120, 260, 260))

        # Confirm the suggested frame without touching it.
        r = confirm_figure(c, tid)
        assert r.status_code == 200, r.text
        d = detail(c, tid)
        fig = d["figure"]
        assert d["status"] == "draft" and fig["confirmed"] is True
        assert fig["page"] == 1 and near(fig["rect"], (60, 120, 260, 260)) and fig["alt"]
        assert fig["material_id"] == c.material_id
        png = (tmp_path / fig["file"]).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # 2x resolution render of a 200 x 140 pt area
        pix = pymupdf.Pixmap(png)
        assert abs(pix.width - 400) <= 3 and abs(pix.height - 280) <= 3
        assert_png_is_region(png, page_pix(figure_pdf("raster")), (120, 240, 520, 520))
        assert row(tmp_path, tid)["revision"] == 2  # the confirmation is a new revision


# -------------------------------------------------------------------- AC-10


def test_ac10(tmp_path):
    # Vector scheme: no raster object -> no suggestion; the parent draws the frame.
    with figure_client(tmp_path / "v", "vector") as c:
        tid = task_ids(tmp_path / "v")[0]
        fig = detail(c, tid)["figure"]
        assert fig["rect"] is None and fig["suggested"] is False
        assert detail(c, tid)["card"]["kind"] == "needs_figure"
        full = page_pix(figure_pdf("vector"))
        box = nonwhite_bbox(page_pix(figure_pdf("vector", text=False)))
        r = confirm_figure(c, tid, rect_px=list(box), scale=2.0)
        assert r.status_code == 200, r.text
        fig = detail(c, tid)["figure"]
        assert near(fig["rect"], (60, 120, 260, 260), tol=2)
        assert_png_is_region(figure_png(c, tid), full, box)

    # Scan page: the whole page is one picture -> not proposed as the figure.
    with figure_client(tmp_path / "s", "scan") as c:
        tid = task_ids(tmp_path / "s")[0]
        fig = detail(c, tid)["figure"]
        assert fig["rect"] is None and fig["suggested"] is False
        full = page_pix(figure_pdf("scan"))
        box = (200, 300, 700, 800)
        assert confirm_figure(c, tid, rect_px=list(box), scale=2.0).status_code == 200
        assert near(detail(c, tid)["figure"]["rect"], (100, 150, 350, 400), tol=2)
        assert_png_is_region(figure_png(c, tid), full, box)

    # Rotated page with a CropBox: pixels -> / scale -> derotation -> CropBox offset.
    expected = (120, 140, 320, 280)  # where the picture was placed, unrotated page space
    full = page_pix(figure_pdf("rotated"))
    box = nonwhite_bbox(page_pix(figure_pdf("rotated", text=False)))
    with figure_client(tmp_path / "r1", "rotated") as c:
        tid = task_ids(tmp_path / "r1")[0]
        auto = detail(c, tid)["figure"]
        assert near(auto["rect"], expected, tol=2), auto["rect"]  # auto-detected, same frame
        assert confirm_figure(c, tid).status_code == 200
        assert_png_is_region(figure_png(c, tid), full, box)
    with figure_client(tmp_path / "r2", "rotated") as c:
        tid = task_ids(tmp_path / "r2")[0]
        assert confirm_figure(c, tid, rect_px=list(box), scale=2.0).status_code == 200
        manual = detail(c, tid)["figure"]["rect"]
        assert near(manual, expected, tol=2), manual  # pixel route lands on the same rect
        assert_png_is_region(figure_png(c, tid), full, box)

    # The same image twice on one page: the instance next to the task is chosen.
    with figure_client(tmp_path / "t", "twice") as c:
        tid = task_ids(tmp_path / "t")[0]
        assert near(detail(c, tid)["figure"]["rect"], (60, 470, 200, 560))


# -------------------------------------------------------------------- AC-11


def _publish(c, tmp_path, tid, *, key="Б", **fig_kw):
    assert confirm_figure(c, tid, **fig_kw).status_code == 200
    r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": key})
    assert r.status_code == 200, r.text


def test_ac11(tmp_path):
    tasks = [fig_task(11), fig_task(12), fig_task(13), fig_task(14)]
    with figure_client(tmp_path, "raster", tasks=tasks) as c:
        a, b, third, ranged = task_ids(tmp_path)
        # teacher_note names 1-5 only; options use 1-6
        _publish(c, tmp_path, a, teacher_note="1 — лепесток, 2, 3, 4, 5 — части цветка")
        # every designation is named, and the parent ticks the box
        _publish(c, tmp_path, b, teacher_note="1, 2, 3, 4, 5 и 6 — части цветка")
        # every designation named but no parent mark
        _publish(c, tmp_path, third, teacher_note="1 2 3 4 5 6", teacher_note_ack=False)

        # a range counts as naming every number in it
        _publish(c, tmp_path, ranged, teacher_note="цифры 1–6 — части цветка")
        assert detail(c, ranged)["publication"]["assist"] == "hints"

        da, db_, dc = detail(c, a), detail(c, b), detail(c, third)
        assert da["publication"]["published"] and da["publication"]["assist"] == "key_only"
        assert "6" in da["publication"]["parent_notice"]
        assert db_["publication"]["assist"] == "hints"
        assert dc["publication"]["assist"] == "key_only"

        # the child sees the mode, never the note
        shown = c.get(f"/api/tasks/{a}", headers=CHILD).json()
        assert shown["assist"] == "key_only"
        assert c.get(f"/api/tasks/{b}", headers=CHILD).json()["assist"] == "hints"
        assert "teacher_note" not in str(shown) and "части цветка" not in str(shown)


# -------------------------------------------------------------------- AC-12


def test_ac12(tmp_path):
    tasks = [fig_task(11, CELL_OPTS)]
    with figure_client(tmp_path, "raster", tasks=tasks) as c:
        tid = task_ids(tmp_path)[0]
        reasons = []
        for kw in (
            {"alt": ""},
            {"alt": "   "},
            {"alt": "На рисунке видна митохондрия"},
            {"no_answer_ack": False},
        ):
            r = confirm_figure(c, tid, **kw)
            assert r.status_code == 422, kw
            reasons.append(r.json()["error"])
            assert detail(c, tid)["figure"]["confirmed"] is False
            assert detail(c, tid)["status"] == "unsupported"
        assert len(set(reasons)) == 3 and all(len(x) > 10 for x in reasons)  # empty alt twice
        assert "митохондрия" in reasons[2]

        # a parent-set key: alt naming the correct designation is refused as well
        c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "Б"})
        r = confirm_figure(c, tid, alt="Верный ответ: Б")
        assert r.status_code == 422 and "Б" in r.json()["error"]
        assert (
            confirm_figure(c, tid, alt="Клетка в разрезе, стрелками показаны части").status_code
            == 200
        )


# -------------------------------------------------------------------- AC-13


def test_ac13(tmp_path):
    with figure_client(tmp_path, "raster") as c:
        tid = task_ids(tmp_path)[0]
        assert confirm_figure(c, tid).status_code == 200
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        assert r.status_code == 200, r.text
        got = row(tmp_path, tid)
        assert got["import_status"] == "checked" and got["model_answer"] is None
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []
        assert c.get(f"/api/tasks/{tid}", headers=CHILD).status_code == 404
        card = detail(c, tid)["card"]
        assert card["kind"] == "no_key" and "ключ" in card["hint"]
        assert c.model.calls_of("task_answer") == []  # never computed for a figure task
        retry = c.post(f"/parent/tasks/{tid}/retry-model", headers=PARENT)
        assert retry.status_code == 422 and c.model.calls_of("task_answer") == []

        # With the parent's own answer the task is published, still without model_answer.
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "В"})
        assert r.status_code == 200, r.text
        assert row(tmp_path, tid)["key_source"] == "parent"
        assert [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]] == [tid]
        assert c.model.calls_of("task_answer") == []


# -------------------------------------------------------------------- AC-14


def test_ac14(tmp_path):
    note = "1 — тычинка, 2 — пестик, 3, 4, 5 и 6 — части околоцветника"
    with figure_client(tmp_path, "raster", tasks=[fig_task(11), fig_task(12)]) as c:
        tid = task_ids(tmp_path)[0]
        _publish(c, tmp_path, tid, teacher_note=note)
        assert c.get(f"/api/tasks/{tid}", headers=CHILD).status_code == 200

        # The model request: the note, no picture, no path.
        system, messages = build_task_request(dict(row(tmp_path, tid)))
        blob = system + str(messages)
        assert note in messages[-1]["content"]
        for forbidden in ("png", "PNG", "figures/", str(tmp_path), "iVBOR", "base64", ".pdf"):
            assert forbidden not in blob, forbidden

        # The child gets no PDF and no page render; the parent does.
        mid = c.material_id
        for url in (f"/materials/{mid}/pdf", f"/materials/{mid}/pages/1.png"):
            assert c.get(url, headers=CHILD).status_code == 403, url
        assert c.get(f"/materials/{mid}/pdf", headers=PARENT).content[:4] == b"%PDF"

        # A second render of the same page comes from the cache.
        cache = c.app.state.page_cache
        before = (cache.hits, cache.misses)
        first = c.get(f"/materials/{mid}/pages/1.png", headers=PARENT)
        second = c.get(f"/materials/{mid}/pages/1.png", headers=PARENT)
        assert first.status_code == second.status_code == 200
        assert first.content == second.content and first.content[:4] == b"\x89PNG"
        assert (cache.misses - before[1], cache.hits - before[0]) == (1, 1)

        # A figure of a task that is not published is not shown to the child at all.
        other = task_ids(tmp_path)[1]
        assert confirm_figure(c, other).status_code == 200  # confirmed, but not checked yet
        assert c.get(f"/tasks/{other}/figure.png", headers=CHILD).status_code == 404
        assert c.get(f"/tasks/{other}/figure.png", headers=PARENT).status_code == 200

        # The child may see the task's own (confirmed) figure, nothing else about it.
        assert c.get(f"/tasks/{tid}/figure.png", headers=CHILD).status_code == 200
        shown = c.get(f"/api/tasks/{tid}", headers=CHILD).json()
        assert "teacher_note" not in shown["figure"] and note not in str(shown)
        assert "rect" not in shown["figure"] and "file" not in shown["figure"]


def _printed_number_pdf(printed="42") -> bytes:
    """Task 11 with a picture on which the number 42 is printed; task 12 follows below."""
    from tests.bank_helpers import doc_bytes, quad_png, write_lines

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(pymupdf.Rect(60, 120, 260, 260), stream=quad_png(), keep_proportion=False)
    page.insert_text((80, 200), printed, fontsize=14, fontname="helv")
    write_lines(page, ["11. Рассмотрите рисунок и выберите верный ответ."], y=400)
    write_lines(page, ["12. Сколько хромосом в клетке?"], y=500)
    return doc_bytes(doc)


def test_a_neighbours_new_key_unpublishes_a_figure_that_prints_it(tmp_path):
    numeric = make_task(12, "Сколько хромосом в клетке?", type="numeric")
    model = StubModel(split=reply(fig_task(11), numeric), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, other = task_ids(tmp_path)
        _publish(c, tmp_path, fig)
        assert c.get(f"/api/tasks/{fig}", headers=CHILD).status_code == 200

        r = c.post(f"/parent/tasks/{other}/key", headers=PARENT, json={"answer": "42"})
        assert r.status_code == 200, r.text
        got = detail(c, fig)
        assert got["figure"]["confirmed"] is False and got["status"] == "unsupported"
        assert c.get(f"/api/tasks/{fig}", headers=CHILD).status_code == 404
        assert c.get(f"/tasks/{fig}/figure.png", headers=CHILD).status_code == 404


def test_an_uploaded_key_file_unpublishes_a_figure_that_prints_its_answer(tmp_path):
    numeric = make_task(12, "Сколько хромосом в клетке?", type="numeric")
    model = StubModel(split=reply(fig_task(11), numeric), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, _ = task_ids(tmp_path)
        _publish(c, tmp_path, fig)
        assert c.get(f"/api/tasks/{fig}", headers=CHILD).status_code == 200

        assert upload(c, key_pdf({"12": "42"}), kind="key", name="k.pdf").status_code == 202
        got = detail(c, fig)
        assert got["figure"]["confirmed"] is False and got["status"] == "unsupported"
        assert c.get(f"/api/tasks/{fig}", headers=CHILD).status_code == 404


def _key_import_material(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT * FROM material WHERE kind = 'key'").fetchall()
    finally:
        conn.close()


def _key_entries(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM key_entry").fetchone()[0]
    finally:
        conn.close()


def test_a_failed_figure_recheck_rolls_the_key_import_back(tmp_path, monkeypatch):
    from app.ingest import figure_service

    numeric = make_task(12, "Сколько хромосом в клетке?", type="numeric")
    model = StubModel(split=reply(fig_task(11), numeric), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, _ = task_ids(tmp_path)
        _publish(c, tmp_path, fig)

        def boom(*a, **kw):
            raise RuntimeError("recheck failed")

        with monkeypatch.context() as patch:
            patch.setattr(figure_service, "recheck_figure", boom)
            upload(c, key_pdf({"12": "42"}), kind="key", name="k.pdf")
        (key,) = _key_import_material(tmp_path)
        assert key["import_status"] == "failed" and _key_entries(tmp_path) == 0
        assert detail(c, fig)["figure"]["confirmed"] is True  # nothing was learned from the key
        assert c.get(f"/api/tasks/{fig}", headers=CHILD).status_code == 200


def test_edited_options_recheck_the_neighbours_figures(tmp_path):
    def opts(first):
        return [{"label": first, "text": "ядро"}, {"label": "40", "text": "рибосома"}]

    single = dict(make_task(12, "Выберите органоид.", options=opts("41")), answer_text="ядро")
    model = StubModel(split=reply(fig_task(11), single), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, other = task_ids(tmp_path)
        _publish(c, tmp_path, fig)  # the page answer of task 12 maps to the label 41
        r = c.post(f"/parent/tasks/{other}/edit", headers=PARENT, json={"options": opts("42")})
        assert r.status_code == 200, r.text
        got = detail(c, fig)
        assert got["figure"]["confirmed"] is False and got["status"] == "unsupported"


def test_an_unrelated_or_unchanged_leak_keeps_the_parents_approval(tmp_path, monkeypatch):
    numeric = dict(make_task(12, "Сколько хромосом в клетке?", type="numeric"), answer_text="42")
    model = StubModel(split=reply(fig_task(11), numeric), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, _ = task_ids(tmp_path)
        assert confirm_figure(c, fig, leak_ack=True).status_code == 200  # 42 is printed, ack'd
        assert detail(c, fig)["figure"]["leak_ack"] is True

        # A key of another year matches nothing; a key for this collection changes the
        # candidate of task 12 but not the text printed in the picture.
        from app.ingest import figure_service

        calls = []
        real = figure_service.recheck_figure
        with monkeypatch.context() as patch:
            patch.setattr(
                figure_service, "recheck_figure", lambda *a, **kw: calls.append(a) or real(*a, **kw)
            )
            upload(c, key_pdf({"12": "43"}), kind="key", year="2030", name="k30.pdf")
        assert calls == []  # no candidate of the 2022 collection changed: nothing is scanned
        upload(c, key_pdf({"12": "43"}), kind="key", name="k.pdf")
        got = detail(c, fig)["figure"]
        assert got["confirmed"] is True and got["leak_ack"] is True


def _boom(*args, **kwargs):
    raise RuntimeError("recheck failed")


def test_a_failed_recheck_leaves_a_new_key_uncommitted_and_the_retry_rechecks(
    tmp_path, monkeypatch
):
    import pytest

    from app.ingest import figure_service

    numeric = make_task(12, "Сколько хромосом в клетке?", type="numeric")
    model = StubModel(split=reply(fig_task(11), numeric), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, other = task_ids(tmp_path)
        _publish(c, tmp_path, fig)
        before = dict(row(tmp_path, other))
        with monkeypatch.context() as patch, pytest.raises(RuntimeError):
            patch.setattr(figure_service, "recheck_figure", _boom)
            c.post(f"/parent/tasks/{other}/key", headers=PARENT, json={"answer": "42"})
        assert dict(row(tmp_path, other)) == before  # no key, no revision, no snapshot
        assert detail(c, fig)["figure"]["confirmed"] is True

        # The same key again is not skipped as «unchanged»: nothing was stored.
        r = c.post(f"/parent/tasks/{other}/key", headers=PARENT, json={"answer": "42"})
        assert r.status_code == 200, r.text
        assert detail(c, fig)["figure"]["confirmed"] is False


def test_a_failed_recheck_leaves_an_options_edit_uncommitted_and_the_retry_rechecks(
    tmp_path, monkeypatch
):
    import pytest

    from app.ingest import figure_service

    def opts(first):
        return [{"label": first, "text": "ядро"}, {"label": "40", "text": "рибосома"}]

    single = dict(make_task(12, "Выберите органоид.", options=opts("41")), answer_text="ядро")
    model = StubModel(split=reply(fig_task(11), single), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, other = task_ids(tmp_path)
        _publish(c, tmp_path, fig)
        before = dict(row(tmp_path, other))
        with monkeypatch.context() as patch, pytest.raises(RuntimeError):
            patch.setattr(figure_service, "recheck_figure", _boom)
            c.post(f"/parent/tasks/{other}/edit", headers=PARENT, json={"options": opts("42")})
        assert dict(row(tmp_path, other)) == before
        assert detail(c, fig)["figure"]["confirmed"] is True

        r = c.post(f"/parent/tasks/{other}/edit", headers=PARENT, json={"options": opts("42")})
        assert r.status_code == 200, r.text
        assert detail(c, fig)["figure"]["confirmed"] is False


def test_an_edit_returns_the_revision_that_is_stored_after_the_recheck(tmp_path):
    tasks = [fig_task(11, CELL_OPTS)]
    with figure_client(tmp_path, "raster", tasks=tasks) as c:
        tid = task_ids(tmp_path)[0]
        assert confirm_figure(c, tid).status_code == 200
        # The new option texts name what the picture shows: the recheck makes another revision.
        options = [{"label": "А", "text": "Рисунок: части объекта обозначены цифрами"}] + CELL_OPTS[
            1:
        ]
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"options": options})
        assert r.status_code == 200, r.text
        assert detail(c, tid)["figure"]["confirmed"] is False  # the recheck did run
        assert r.json()["revision"] == row(tmp_path, tid)["revision"]


class _OpenSpy:
    """Stands in for the pymupdf module of figure_service: records every open of a file and
    whether another connection could take the write lock at that moment."""

    def __init__(self, tmp_path):
        self.opens, self.db_path = [], tmp_path / "olymp.db"

    def __getattr__(self, name):
        return getattr(pymupdf, name)

    def open(self, *args, **kwargs):
        import sqlite3

        probe = sqlite3.connect(str(self.db_path), timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            self.opens.append("free")
        except sqlite3.OperationalError:
            self.opens.append("locked")
        finally:
            probe.close()
        return pymupdf.open(*args, **kwargs)


def test_a_recheck_opens_each_pdf_once_and_outside_the_write_lock(tmp_path, monkeypatch):
    from app.ingest import figure_service

    def opts(first):
        return [{"label": first, "text": "ядро"}, {"label": "40", "text": "рибосома"}]

    single = dict(make_task(12, "Выберите органоид.", options=opts("41")), answer_text="ядро")
    model = StubModel(split=reply(fig_task(11), single), answer=answer_by_number({}))
    app = build_app(tmp_path, model)
    with TestClient(app) as c:
        assert upload(c, _printed_number_pdf()).status_code == 202
        fig, other = task_ids(tmp_path)
        _publish(c, tmp_path, fig)
        spy = _OpenSpy(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(figure_service, "pymupdf", spy)
            r = c.post(f"/parent/tasks/{other}/key", headers=PARENT, json={"answer": "40"})
            assert r.status_code == 200, r.text
        assert spy.opens == ["free"]  # one open for the whole material, before BEGIN
        assert detail(c, fig)["figure"]["confirmed"] is True

        spy.opens.clear()
        with monkeypatch.context() as patch:
            patch.setattr(figure_service, "pymupdf", spy)
            r = c.post(f"/parent/tasks/{other}/edit", headers=PARENT, json={"options": opts("42")})
            assert r.status_code == 200, r.text
        assert spy.opens == ["free"]
        assert detail(c, fig)["figure"]["confirmed"] is False


def test_a_busy_database_answers_503_not_500(tmp_path, monkeypatch):
    import sqlite3

    from app.ingest import review

    app = build_app(tmp_path, StubModel(split=reply(fig_task(11)), answer=answer_by_number({})))
    with TestClient(app) as c:
        assert upload(c, figure_pdf("raster")).status_code == 202
        (tid,) = task_ids(tmp_path)

        def busy(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as patch:
            patch.setattr(review, "set_parent_key", busy)
            r = c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 503 and "ещё раз" in r.json()["error"]
        assert r.headers["retry-after"]


def _bank_with_figures(c, tmp_path, pages=12):
    """A textbook-kind material of `pages` pages with two confirmed figures per page, and answers
    that overlap the printed labels in every direction: equal, the label inside a longer answer,
    several answer pieces, a stored key, and no overlap at all."""
    from tests.bank_helpers import doc_bytes, quad_png, write_lines

    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=600, height=800)
        write_lines(page, [f"Страница {n + 1}"], y=60)
        page.insert_image(pymupdf.Rect(60, 100, 260, 240), stream=quad_png(), keep_proportion=False)
        label = f"{n % 5} mark" if n % 2 == 0 else f"see {n % 5} mark here"
        page.insert_text((80, 160), label, fontsize=12, fontname="helv")
        page.insert_image(pymupdf.Rect(60, 400, 260, 540), stream=quad_png(), keep_proportion=False)
    material = upload(c, doc_bytes(doc), kind="textbook").json()["material_id"]
    conn = db(tmp_path)
    ids = []
    for i in range(pages * 2):
        page, k = i // 2 + 1, i % 5
        frame = [60, 100, 260, 240] if i % 2 == 0 else [60, 400, 260, 540]
        figure = {
            "material_id": material,
            "page": page,
            "rect": frame,
            "regions": [{"page": page, "rect": frame}],
            "confirmed": True,
            "alt": "Рисунок к заданию",
            "leak": [],
            "leak_ack": False,
            "teacher_note": "",
        }
        answer = [
            f"{k} mark",
            f"а б {k} mark в",
            "ничего общего",
            f"{k} mark\nвторая строка",
        ][i % 4]
        key = ('{"type": "numeric", "value": "2 mark"}', "parent") if i % 7 == 0 else (None, None)
        cur = conn.execute(
            "INSERT INTO task_item (material_id, page_from, page_to, source_number, type, text, "
            "answer_text, figure, answer_key, key_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                material,
                page,
                page,
                str(i + 1),
                "numeric",
                f"Задание {i + 1}",
                answer,
                json.dumps(figure, ensure_ascii=False),
                *key,
            ),
        )
        ids.append(cur.lastrowid)
    conn.close()
    return material, ids


def test_the_material_leak_index_gives_the_same_verdicts_and_is_built_once(tmp_path, monkeypatch):
    from app.ingest import figure_service
    from app.ingest.figures import crop_leaks

    app = build_app(tmp_path, StubModel(split=reply(), answer=answer_by_number({})))
    with TestClient(app) as c:
        material, ids = _bank_with_figures(c, tmp_path)
        ctx = c.app.state.ctx
        conn = db(tmp_path)
        shared = figure_service._Shared(ctx, conn, material)
        doc = pymupdf.open(
            str(
                ctx.material_path(
                    conn.execute("SELECT file FROM material WHERE id = ?", (material,)).fetchone()[
                        "file"
                    ]
                )
            )
        )
        verdicts = []
        for tid in ids:
            task = conn.execute("SELECT * FROM task_item WHERE id = ?", (tid,)).fetchone()
            regions = json.loads(task["figure"])["regions"]
            new = shared.leaks(task, regions)
            old = crop_leaks(doc, regions, figure_service.leak_units(conn, task))
            assert sorted(new) == sorted(old), (tid, new, old)
            verdicts.append(bool(new))
        shared.close()
        doc.close()
        conn.close()
        assert any(verdicts) and not all(verdicts)  # the cases really overlap, and not always

        builds = []
        real = figure_service._build_leak_index
        with monkeypatch.context() as patch:
            patch.setattr(
                figure_service, "_build_leak_index", lambda f: builds.append(1) or real(f)
            )
            r = c.post(f"/parent/tasks/{ids[1]}/key", headers=PARENT, json={"answer": "7"})
            assert r.status_code == 200, r.text
        assert builds == [1]  # one index for the material, however many figures it has


# -------------------------------------------------------------------- AC-22


def test_a_figure_with_unreadable_labels_is_unsupported(tmp_path):
    # 2.5 pt labels on a 200 pt wide figure shown 560 px wide: 7 px tall, unreadable.
    with figure_client(tmp_path / "tiny", "raster", labels_size=2.5) as c:
        tid = task_ids(tmp_path / "tiny")[0]
        r = confirm_figure(c, tid)
        assert r.status_code == 200, r.text
        d = detail(c, tid)
        assert d["status"] == "unsupported" and "мельче" in d["card"]["hint"]
        assert c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={}).status_code == 422
        assert c.get("/api/tasks", headers=CHILD).json()["tasks"] == []
        assert row(tmp_path / "tiny", tid)["import_status"] == "unsupported"

    # 12 pt labels are readable: the same flow ends in draft.
    with figure_client(tmp_path / "ok", "raster", labels_size=12) as c:
        tid = task_ids(tmp_path / "ok")[0]
        assert confirm_figure(c, tid).status_code == 200
        assert detail(c, tid)["status"] == "draft"


# -------------------------------------------------------------------- AC-23


def test_the_page_cache_evicts_the_oldest_page_at_the_limit(tmp_path):
    assert CACHE_LIMIT_BYTES == 500 * 1024 * 1024
    root = tmp_path / "cache"
    root.mkdir()
    cache = PageCache(root)
    chunk = 100 * 1024 * 1024
    now = time.time()
    names = []
    for i in range(5):  # 5 x 100 MB (sparse): the cache is exactly full
        p = root / f"old{i}.png"
        with open(p, "wb") as f:
            f.truncate(chunk)
        os.utime(p, (now - 1000 + i, now - 1000 + i))
        names.append(p)
    assert sum(p.stat().st_size for p in root.iterdir()) == CACHE_LIMIT_BYTES

    data = cache.get("new-page", lambda: b"\x89PNG" + b"x" * 5000)
    assert data.startswith(b"\x89PNG")
    files = {p.name for p in root.iterdir()}
    assert "old0.png" not in files  # the oldest page is gone
    assert {"old1.png", "old2.png", "old3.png", "old4.png"} <= files
    assert sum(p.stat().st_size for p in root.iterdir()) <= CACHE_LIMIT_BYTES
    assert len(files) == 5  # new page present
    # a hit touches the entry: it becomes the youngest
    assert cache.get("new-page", lambda: b"never") == data
    assert Path(root).exists()


def test_every_figure_edit_makes_a_revision_and_unchecks(tmp_path):
    with figure_client(tmp_path, "raster") as c:
        tid = task_ids(tmp_path)[0]
        _publish(c, tmp_path, tid, teacher_note="1 2 3 4 5 6")
        assert row(tmp_path, tid)["import_status"] == "checked"

        def state():
            r = row(tmp_path, tid)
            return r["revision"], r["import_status"], r["model_answer"]

        rev, _, _ = state()
        # teacher_note
        assert (
            c.post(
                f"/parent/tasks/{tid}/edit",
                headers=PARENT,
                json={"teacher_note": "1 2 3 4 5 6 — части"},
            ).status_code
            == 200
        )
        assert state() == (rev + 1, "draft", None)
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        # alt (re-confirming the figure)
        assert confirm_figure(c, tid, alt="Другое описание рисунка").status_code == 200
        assert state()[:2] == (rev + 2, "draft")
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        # frame
        assert confirm_figure(c, tid, rect=[70, 130, 250, 250]).status_code == 200
        assert state()[:2] == (rev + 3, "draft")
        # the old PNG file is replaced by the one of the new revision
        files = sorted(p.name for p in (tmp_path / "figures").iterdir())
        assert files == [f"{tid}-r{rev + 3}.png"]


def test_alt_rejects_short_option_texts_as_whole_words():
    from app.ingest.figures import alt_problem

    options = [
        {"label": "А", "text": "ДНК"},
        {"label": "Б", "text": "РНК"},
        {"label": "В", "text": "АТФ"},
        {"label": "Г", "text": "белок"},
    ]
    for text in ("ДНК", "РНК", "АТФ", "белок"):
        problem = alt_problem(f"На схеме показана {text}", options, ["А"])
        assert problem and text in problem, text
    assert alt_problem("на схеме показана днк.", options, [])  # case and punctuation
    # no option text as a whole word: passes (a longer word containing it is not a hit)
    assert alt_problem("Схема строения клетки с ядром", options, ["А"]) is None
    assert alt_problem("Схема показывает действие РНКазы", options, []) is None
    # numeric option texts are compared too: strictness wins, the parent rephrases
    for option, alt in (
        ("46", "На графике число 46"),
        ("36,6", "Температура 36,6 градуса"),
        ("1, 2", "Части обозначены цифрами 1, 2"),
    ):
        problem = alt_problem(alt, [{"label": "А", "text": option}], [])
        assert problem and option in problem, option
    digit_opts = [{"label": "А", "text": "1, 2"}, {"label": "Б", "text": "1, 6"}]
    assert alt_problem("Части обозначены цифрами 1–6", digit_opts, [])  # «1, 6» is an option
    assert alt_problem("Части обозначены цифрами", digit_opts, []) is None
    # a longer number is not a hit for a shorter option, and the reverse
    assert alt_problem("Число 146", [{"label": "А", "text": "46"}], []) is None
    # a single-character option text is a designation, not compared as text
    assert (
        alt_problem(
            "Показана часть 1 и буква в",
            [{"label": "А", "text": "1"}, {"label": "Б", "text": "в"}],
            [],
        )
        is None
    )


def test_key_and_options_edit_work_after_the_material_file_is_deleted(tmp_path):
    with figure_client(tmp_path, "raster") as c:
        tid = task_ids(tmp_path)[0]
        assert confirm_figure(c, tid).status_code == 200
        assert (
            c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"}).status_code
            == 200
        )  # checked: the deletion keeps the task and its figure
        r = c.post(
            f"/parent/materials/{c.material_id}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert detail(c, tid)["figure"]["confirmed"] is True
        r = c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "В"})
        assert r.status_code == 200, r.text
        assert "В" in row(tmp_path, tid)["answer_key"]
        new_opts = [dict(o) for o in DIGIT_OPTS]
        new_opts[0] = {"label": "А", "text": "2, 3"}
        r = c.post(f"/parent/tasks/{tid}/edit", headers=PARENT, json={"options": new_opts})
        assert r.status_code == 200, r.text
        assert detail(c, tid)["figure"]["confirmed"] is True


def test_after_the_pdf_is_deleted_a_key_change_still_rechecks_the_alt(tmp_path):
    """Review finding (T08 gen 2): skipping the recheck when the PDF is gone also skipped the alt
    check, and the child saw the answer letter in the picture description."""
    with figure_client(tmp_path, "raster") as c:
        tid = task_ids(tmp_path)[0]
        alt = "Стрелка указывает на часть В органа"
        assert confirm_figure(c, tid, alt=alt).status_code == 200
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200, r.text
        r = c.post(
            f"/parent/materials/{c.material_id}/delete",
            headers=PARENT,
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = c.post(f"/parent/tasks/{tid}/key", headers=PARENT, json={"answer": "В"})
        assert r.status_code == 200, r.text
        assert detail(c, tid)["figure"]["confirmed"] is False
        assert row(tmp_path, tid)["import_status"] == "unsupported"
        c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={})
        page = c.get(f"/tasks/{tid}", headers=CHILD)
        assert alt not in page.text
