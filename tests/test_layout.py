"""A collection where the answer stands on the task's own page, in a table (OH-T05b)."""

import json
import re

import pymupdf
from fastapi.testclient import TestClient

from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    build_app,
    db,
    reply,
    task_ids,
    upload,
)
from tests.layout_pdf import (
    CAPTION_PHOTOS,
    OPTIONS_1_1,
    OPTIONS_1_2,
    PHOTOS_P1,
    PHOTOS_P2,
    STATEMENTS,
    layout_pdf,
)

S1 = "Вариант 1. Отборочный этап"
S2 = "Вариант 2. Отборочный этап"
DIGITS = [{"label": str(i), "text": text} for i, text in enumerate(STATEMENTS, 1)]
EVENTS = [{"label": str(i), "text": f"Событие номер {i}"} for i in range(1, 9)]
MATCH_6 = {
    "left": [{"label": str(i), "text": f"Фотография {i}"} for i in range(1, 6)],
    "right": [
        {"label": "А", "text": "Корень"},
        {"label": "Б", "text": "Стебель"},
        {"label": "В", "text": "Лист"},
    ],
}
ROOT_OPTS = [
    {"label": c, "text": t} for c, t in zip("АБВГ", ("корень", "стебель", "лист", "цветок"))
]
ACK = {"no_answer_ack": True, "teacher_note_ack": True}
ALT = "Фотографии животных в таблице"

# What a correct splitter returns: the condition WITHOUT the answers, and the answer apart.
FORBIDDEN = ["1 Стебель", "10 баллов", "№ 2 … № 7", "2 4 6"]


def lt(number, condition, kind, pages=(1,), answer_text=None, options=None, section=S1, **extra):
    task = {
        "number": number,
        "type": kind,
        "condition": condition,
        "options": options,
        "pages": list(pages),
        "section": section,
        "refers_to_figure": False,
    }
    if answer_text is not None:
        task["answer_text"] = answer_text
    task.update(extra)
    return task


def good_tasks():
    return [
        lt(
            "1.1",
            "Рассмотрите фотографии в таблице и выберите животных, относящихся к моллюскам.",
            "multiple",
            pages=(1, 2),
            answer_text="Б, Д",
            options=OPTIONS_1_1,
            refers_to_figure=True,
        ),
        lt(
            "1.2",
            "Определите органоид, показанный на микрофотографии.",
            "single",
            pages=(2,),
            answer_text="Б",
            options=OPTIONS_1_2,
            refers_to_figure=True,
        ),
        lt(
            "1.3",
            "Сколько хромосом содержит соматическая клетка организма? Ответ запишите числом.",
            "numeric",
            pages=(2,),
            answer_text="28",
        ),
        lt("1.4", "Выберите верные суждения.", "multiple", (2,), "2 4 6", DIGITS),
        lt(
            "1.5",
            "Расположите события в порядке их наступления.",
            "multiple",
            (3,),
            "№ 2 … № 7",
            EVENTS,
        ),
        lt(
            "1.6",
            "Определите орган растения по описанию в каждой строке.",
            "match",
            (3,),
            "1 Стебель 2 Лист 3 Корень 4 Стебель 5 Корень 10 баллов",
            MATCH_6,
        ),
        lt(
            "1.7",
            "Нарисуйте схему строения сердца человека и подпишите камеры.",
            "open",
            (3,),
            "Элементы ответа: 1) нарисованы четыре камеры (1 балл) 2) подписаны клапаны (1 балл)",
        ),
        lt(
            "1.8",
            "Объясните, почему растения нуждаются в свете.",
            "open",
            (3,),
            "фотосинтез (1 балл), хлорофилл (1 балл)",
            points=2,
        ),
        lt(
            "1.1",
            "Какой орган растения поглощает воду из почвы?",
            "single",
            (4,),
            "корень",
            ROOT_OPTS,
            section=S2,
        ),
        lt(
            "1.3",
            "Сколько пар хромосом у человека? Ответ запишите числом.",
            "numeric",
            (4,),
            "23",
            section=S2,
        ),
    ]


def import_layout(tmp_path, tasks=None, answer=None):
    model = StubModel(split=reply(*(tasks or good_tasks())), answer=answer)
    app = build_app(tmp_path, model)
    client = TestClient(app)
    client.__enter__()
    resp = upload(client, layout_pdf())
    assert resp.status_code == 202, resp.text
    status = client.get(f"/parent/materials/{resp.json()['material_id']}", headers=PARENT).json()
    assert status["import_status"] == "done", status
    client.model = model
    return client


def by_number(tmp_path, number, section=S1):
    conn = db(tmp_path)
    try:
        return conn.execute(
            "SELECT * FROM task_item WHERE source_number = ? AND section = ?", (number, section)
        ).fetchone()
    finally:
        conn.close()


def detail(client, task_id):
    r = client.get(f"/parent/tasks/{task_id}", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, task_id, **body):
    return client.post(f"/parent/tasks/{task_id}/confirm", headers=PARENT, json=body)


def confirm_figure(client, task_id, **kw):
    body = {"alt": ALT, "teacher_note": "1 2 3 4 5", **ACK, **kw}
    return client.post(f"/parent/tasks/{task_id}/figure", headers=PARENT, json=body)


def child_surfaces(client, query_words):
    """Everything a child can read: lists, cards, JSON, search; as one text."""
    parts = [client.get(p, headers=CHILD).text for p in ("/tasks", "/api/tasks")]
    for task in client.get("/api/tasks", headers=CHILD).json()["tasks"]:
        for path in (f"/tasks/{task['id']}", f"/api/tasks/{task['id']}"):
            parts.append(client.get(path, headers=CHILD).text)
    for word in query_words:
        parts.append(client.get("/api/search", params={"q": word}, headers=CHILD).text)
    return "\n".join(parts)


def has_number(text, number):
    return re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.,])", text) is not None


def crop_text(pdf_bytes, page_no, rect):
    doc = pymupdf.open("pdf", pdf_bytes)
    try:
        return doc[page_no - 1].get_text("text", clip=pymupdf.Rect(*rect))
    finally:
        doc.close()


def union(rects):
    out = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out


def near(rect, expected, tol=2.0):
    return rect is not None and all(abs(a - b) <= tol for a, b in zip(rect, expected, strict=True))


# ------------------------------------------------------------------ extraction


def test_extraction_keeps_rows_and_cells():
    from app.ingest.extract import extract_pages

    pages = dict(extract_pages(layout_pdf()))
    one = pages[1].splitlines()
    assert "[рисунок] | минога" in one and "[рисунок] | пиявка" in one
    assert (
        "минога" not in one and one.count("[рисунок] | минога") == 1
    )  # the cell text is not doubled
    assert one.index("[рисунок] | минога") < one.index("[рисунок] | улитка")
    assert any(line.startswith("1.1 Рассмотрите") for line in one)
    assert one.index(next(x for x in one if x.startswith("1.1 Рассмотрите"))) < one.index(
        "[рисунок] | минога"
    )
    assert "Ответ | 28" in pages[2].splitlines() and "Ответ | 2 4 6" in pages[2].splitlines()
    assert "Ответ | № 2 … № 7" in pages[3].splitlines()


# ------------------------------------------------------------------------ AC-1


def test_ac1(tmp_path):
    with import_layout(tmp_path) as client:
        # Both sections keep their numbers; the in-page answer is stored apart, as a candidate.
        assert len(task_ids(tmp_path)) == 10
        t13 = by_number(tmp_path, "1.3")
        assert t13["answer_text"] == "28" and t13["answer_key"] is None
        assert "28" not in t13["text"]
        d = detail(client, t13["id"])
        assert d["key"] is None and d["key_source"] is None
        assert d["key_candidate"]["answer"] == "28"
        assert detail(client, by_number(tmp_path, "1.6")["id"])["key_candidate"]["answer"]
        assert not client.get("/api/tasks", headers=CHILD).json()["tasks"]

        # The model got the cells of the tables with their rows.
        content = client.model.calls_of("tasks_split")[0].messages[-1]["content"]
        assert "[рисунок] | минога" in content and "Ответ | 28" in content

        # Official only after the parent confirms.
        t11 = by_number(tmp_path, "1.1")
        assert confirm_figure(client, t11["id"]).status_code == 200
        for number, section in (
            ("1.1", S1),
            ("1.3", S1),
            ("1.4", S1),
            ("1.5", S1),
            ("1.6", S1),
            ("1.1", S2),
            ("1.3", S2),
        ):
            task = by_number(tmp_path, number, section)
            r = confirm(client, task["id"])
            assert r.status_code == 200, (number, r.text)
            after = detail(client, task["id"])
            assert after["key_source"] == "official" and after["key"], (number, after)
        assert detail(client, t13["id"])["key"] == "28"
        assert by_number(tmp_path, "1.3", S2)["answer_text"] == "23"

        # No child surface carries an answer token.
        blob = child_surfaces(client, ["хромосом", "соматическая", "строке", "суждения"])
        assert len(client.get("/api/tasks", headers=CHILD).json()["tasks"]) == 7
        for token in FORBIDDEN:
            assert token not in blob, token
        assert not has_number(blob, "28") and not has_number(blob, "23")

        # The figure crop: the text inside the confirmed frames has no answer and no option.
        figure = detail(client, t11["id"])["figure"]
        for region in figure["regions"]:
            text = crop_text(layout_pdf(), region["page"], region["rect"])
            assert not any(name in text for name in ("минога", "улитка", "осьминог")), text


def test_a_condition_that_still_holds_the_answer_is_warned_and_cannot_be_published(tmp_path):
    leaking = good_tasks()
    leaking[2]["condition"] += " Ответ: 28"
    leaking[3]["condition"] += " Верно: 2 4 6"
    leaking[5]["condition"] += " Например, 1 Стебель."  # one pair of a longer answer
    with import_layout(tmp_path, leaking) as client:
        for number in ("1.3", "1.4", "1.6"):
            task = by_number(tmp_path, number)
            d = detail(client, task["id"])
            assert d["publication"]["blocked"], number
            assert d["card"]["warning"], number
            r = confirm(client, task["id"])
            assert r.status_code == 422, (number, r.text)
            assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404
        # The parent edits the condition: the warning goes, the task can be published.
        task = by_number(tmp_path, "1.3")
        clean = "Сколько хромосом содержит соматическая клетка организма?"
        r = client.post(f"/parent/tasks/{task['id']}/edit", headers=PARENT, json={"text": clean})
        assert r.status_code == 200, r.text
        assert not detail(client, task["id"])["publication"]["blocked"]
        assert confirm(client, task["id"]).status_code == 200
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 200

    # A checked task whose condition regains the answer is not shown, whatever the status.
    with import_layout(tmp_path / "again") as client:
        task = by_number(tmp_path / "again", "1.3")
        assert confirm(client, task["id"]).status_code == 200
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 200
        conn = db(tmp_path / "again")
        conn.execute("UPDATE task_item SET text = text || ' Ответ 28' WHERE id = ?", (task["id"],))
        conn.close()
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404
        assert not client.get("/api/search", params={"q": "хромосом"}, headers=CHILD).json()[
            "results"
        ]


def test_a_page_answer_that_does_not_fit_the_task_is_not_a_candidate(tmp_path):
    tasks = good_tasks()
    tasks[3]["answer_text"] = "Ответ приведён в таблице"
    tasks[4]["answer_text"] = "№ 12"
    with import_layout(tmp_path, tasks) as client:
        for number in ("1.4", "1.5"):
            d = detail(client, by_number(tmp_path, number)["id"])
            assert d["key_candidate"] is None, number
            assert d["card"]["kind"] == "no_key"


# ------------------------------------------------------------------------ AC-2


def test_ac2(tmp_path):
    with import_layout(tmp_path) as client:
        t11 = by_number(tmp_path, "1.1")
        figure = detail(client, t11["id"])["figure"]
        # All the photos of the table, one area per page; the photos of the next task stay out.
        pages = [r["page"] for r in figure["regions"]]
        assert pages == [1, 2], figure
        assert near(figure["regions"][0]["rect"], union(PHOTOS_P1))
        assert near(figure["regions"][1]["rect"], union(PHOTOS_P2))
        assert figure["page"] == 1 and figure["leak"] == []

        # A frame over the whole table holds the names: blocked until the parent decides.
        table = [50, 130, 400, 340]
        r = confirm_figure(client, t11["id"], rect=table)
        assert r.status_code == 422, r.text
        assert "минога" in r.json()["error"]
        assert not detail(client, t11["id"])["figure"]["confirmed"]

        # The suggestion itself is clean and covers both pages in one picture.
        assert confirm_figure(client, t11["id"]).status_code == 200
        done = detail(client, t11["id"])["figure"]
        assert done["confirmed"] and len(done["regions"]) == 2
        png = pymupdf.Pixmap((tmp_path / done["file"]).read_bytes())
        assert abs(png.width - 2 * union(PHOTOS_P1).width) <= 3
        assert png.height >= 2 * (union(PHOTOS_P1).height + union(PHOTOS_P2).height) - 3

        # The explicit decision of the parent is kept with the figure.
        r = confirm_figure(client, t11["id"], rect=table, leak_ack=True)
        assert r.status_code == 200, r.text
        decided = detail(client, t11["id"])["figure"]
        assert decided["leak_ack"] is True and "минога" in decided["leak"]
        assert len(decided["regions"]) == 1

        # A caption of the answer lies between two photos of the next task: the proposal is
        # flagged, and the confirmation needs an explicit decision.
        t12 = by_number(tmp_path, "1.2")
        fig12 = detail(client, t12["id"])["figure"]
        assert [r["page"] for r in fig12["regions"]] == [2]
        assert near(fig12["regions"][0]["rect"], union(CAPTION_PHOTOS))
        assert "рибосома" in " ".join(fig12["leak"])
        r = confirm_figure(client, t12["id"])
        assert r.status_code == 422 and "рибосома" in r.text
        assert not detail(client, t12["id"])["figure"]["confirmed"]
        assert confirm_figure(client, t12["id"], leak_ack=True).status_code == 200
        assert detail(client, t12["id"])["figure"]["confirmed"]


def test_the_crop_guard_compares_text_in_the_frame_with_the_key_and_the_options():
    from app.ingest.figures import crop_leaks

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        page2 = [{"page": 2, "rect": [50, 510, 400, 536]}]
        assert crop_leaks(doc, page2, ["28"]) == ["28"]  # the key
        assert crop_leaks(doc, page2, ["29"]) == []
        caption = [{"page": 2, "rect": [60, 340, 220, 385]}]
        assert crop_leaks(doc, caption, ["рибосома"]) == ["рибосома"]  # an option text
        assert crop_leaks(doc, [{"page": 2, "rect": [60, 560, 70, 570]}], ["рибосома"]) == []
        assert crop_leaks(doc, [{"page": 1, "rect": [50, 130, 400, 340]}], ["УЛИТКА"]) == [
            "улитка"
        ]  # case does not matter
        # A single character or a bare designation is not an answer text.
        assert crop_leaks(doc, [{"page": 3, "rect": [0, 0, 595, 842]}], ["А", "", "1"]) == []
        # A line of the frame that is a part of a longer answer is the answer too.
        row = [{"page": 3, "rect": [50, 275, 300, 301]}]
        assert crop_leaks(doc, row, ["1 Стебель 2 Лист 3 Корень"]) == ["1 стебель 2 лист 3 корень"]
    finally:
        doc.close()


# ------------------------------------------------------------------------ AC-3


def test_ac3(tmp_path):
    from app.ingest.answers import normalize_answer, same_answer

    assert normalize_answer(" 28 ", "numeric") == "28"
    assert normalize_answer("28,0", "numeric") == "28"
    assert normalize_answer("28.50", "numeric") == "28.5"
    assert normalize_answer("двадцать", "numeric") is None
    assert normalize_answer("1e999", "numeric") is None
    assert normalize_answer("nan", "numeric") is None
    for given in ("28", " 28 ", "28,0", "28.0", "+28"):
        assert same_answer("numeric", "28", given), given
    for given in ("27", "28,5", "", "2 8", "28а", None):
        assert not same_answer("numeric", "28", given), given
    assert same_answer("numeric", "0", "-0") and not same_answer("numeric", "28", "0")

    with import_layout(tmp_path) as client:
        task = by_number(tmp_path, "1.3")
        assert task["type"] == "numeric" and task["import_status"] == "draft"
        assert task["unsupported_reason"] is None
        assert confirm(client, task["id"]).status_code == 200
        stored = json.loads(by_number(tmp_path, "1.3")["answer_key"])
        assert stored == {"type": "numeric", "value": "28"}
        for given, ok in (("28", True), (" 28 ", True), ("28,0", True), ("27", False)):
            assert same_answer("numeric", stored["value"], given) is ok, given
        # The parent may type the key in her own way.
        other = by_number(tmp_path, "1.3", S2)
        r = client.post(f"/parent/tasks/{other['id']}/key", headers=PARENT, json={"answer": "23,0"})
        assert r.status_code == 200, r.text
        assert detail(client, other["id"])["key"] == "23"
        bad = client.post(f"/parent/tasks/{other['id']}/key", headers=PARENT, json={"answer": "x"})
        assert bad.status_code == 422
        view = client.get(f"/api/tasks/{task['id']}", headers=CHILD)
        assert view.status_code == 200 and view.json()["type"] == "numeric"


def test_numeric_options_are_not_required(tmp_path):
    from app.ingest.schema import InvalidStructure, validate_tasks

    ok = validate_tasks({"tasks": [lt("5", "Сколько?", "numeric")]}, {1})
    assert ok[0]["type"] == "numeric" and ok[0]["options"] is None
    for bad in (
        lt("5", "Сколько?", "numeric", answer_text=5),
        lt("5", "", "numeric"),
    ):
        try:
            validate_tasks({"tasks": [bad]}, {1})
        except InvalidStructure:
            continue
        raise AssertionError(bad)


# ------------------------------------------------------------------------ AC-4


def test_ac4(tmp_path):
    with import_layout(tmp_path) as client:
        draw = by_number(tmp_path, "1.7")
        scored = by_number(tmp_path, "1.8")
        for task in (draw, scored):
            assert task["import_status"] == "unsupported" and task["unsupported_reason"]
        assert "рисован" in draw["unsupported_reason"]
        assert "балл" in scored["unsupported_reason"]
        assert draw["unsupported_reason"] != scored["unsupported_reason"]
        # «Элементы ответа» stay as a note for the parent and are not in the condition.
        assert "Элементы ответа" not in draw["text"] and "(1 балл)" not in draw["text"]
        assert "Элементы ответа" in draw["parent_note"]
        assert "(1 балл)" in draw["parent_note"]
        assert draw["answer_text"] is None and scored["answer_text"] is None
        assert "фотосинтез" in scored["parent_note"] and "фотосинтез" not in scored["text"]
        d = detail(client, draw["id"])
        assert d["card"]["parent_note"] == draw["parent_note"]
        assert d["key_candidate"] is None and d["key"] is None
        assert confirm(client, draw["id"]).status_code == 422
        assert client.get(f"/api/tasks/{draw['id']}", headers=CHILD).status_code == 404

    # A splitter that leaves «Элементы ответа» inside the condition, and a drawing task it
    # took for a choice: the server cuts the part off and still refuses the task.
    tasks = good_tasks()
    tasks[6] = lt(
        "1.7",
        "Нарисуйте схему строения сердца человека. Элементы ответа: 1) четыре камеры (1 балл)",
        "single",
        (3,),
        options=OPTS_AB,
    )
    with import_layout(tmp_path / "raw", tasks) as client:
        draw = by_number(tmp_path / "raw", "1.7")
        assert draw["import_status"] == "unsupported" and "рисован" in draw["unsupported_reason"]
        assert draw["text"] == "Нарисуйте схему строения сердца человека."
        assert "четыре камеры" in draw["parent_note"]
        assert detail(client, draw["id"])["figure"] is None


OPTS_AB = [{"label": "А", "text": "один"}, {"label": "Б", "text": "два"}]


# --------------------------------------------------------------- other places of the rules


def test_an_uploaded_key_file_goes_before_the_answer_on_the_page(tmp_path):
    from tests.bank_helpers import key_pdf

    with import_layout(tmp_path) as client:
        assert upload(client, key_pdf({"1.3": "30"}), kind="key", name="k.pdf").status_code == 202
        first = detail(client, by_number(tmp_path, "1.3")["id"])["key_candidate"]
        assert first["answer"] == "30" and first["source"] == "key_file"
        other = detail(client, by_number(tmp_path, "1.3", S2)["id"])["key_candidate"]
        assert other["answer"] == "23" and other["source"] == "page"


def test_the_page_answer_found_in_the_second_chunk_is_kept(tmp_path):
    from tests.bank_helpers import text_pdf

    def split(content):
        task = lt(
            "12", "Сколько хромосом у человека?", "numeric", (2 if "[[стр. 2]]" in content else 1,)
        )
        if "[[стр. 2]]" in content:
            task["answer_text"] = "46"
        return reply(task)

    model = StubModel(split=split)
    with TestClient(build_app(tmp_path, model)) as client:
        long = "а" * 7000
        upload(client, text_pdf([["12. " + long], ["12. " + long]]))
        rows = db(tmp_path).execute("SELECT answer_text FROM task_item").fetchall()
        assert [r["answer_text"] for r in rows] == ["46"]


def test_the_splitter_is_told_to_keep_the_answer_out_of_the_condition():
    from app.ingest.prompt import SYSTEM_PROMPT, TASKS_SCHEMA

    item = TASKS_SCHEMA["properties"]["tasks"]["items"]
    assert "condition" in item["required"] and "text" not in item["properties"]
    assert "answer_text" in item["properties"] and "numeric" in item["properties"]["type"]["enum"]
    assert "answer_text" in SYSTEM_PROMPT and "condition" in SYSTEM_PROMPT


def test_a_figure_is_not_proposed_for_images_of_the_next_task(tmp_path):
    from app.ingest import figures

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        text = "Определите органоид, показанный на микрофотографии."
        only = figures.suggest_regions(doc, 2, text, 2, 2, "Сколько хромосом содержит соматическая")
        assert [r["page"] for r in only] == [2] and near(only[0]["rect"], union(CAPTION_PHOTOS))
        # No next task: everything below the anchor up to the last page of the task.
        until_end = figures.suggest_regions(doc, 2, text, 2)
        assert near(until_end[0]["rect"], union(CAPTION_PHOTOS))
        # The anchor is not on the page: the nearest image is the last resort.
        lost = figures.suggest_regions(doc, 2, "Нет такого текста на странице", 2)
        assert lost and lost[0]["page"] == 2
        # A page without images gives no proposal.
        assert figures.suggest_regions(doc, 4, "Какой орган растения поглощает воду", 4) == []
    finally:
        doc.close()


def test_the_key_typed_by_the_parent_is_checked_against_the_frame_too(tmp_path):
    tasks = good_tasks()
    tasks[2] = lt(
        "1.3",
        "Сколько хромосом содержит соматическая клетка? Смотрите таблицу.",
        "numeric",
        (2,),
        refers_to_figure=True,
    )
    with import_layout(tmp_path, tasks) as client:
        task = by_number(tmp_path, "1.3")
        cell = [150, 510, 400, 536]  # the cell with 28
        assert confirm_figure(client, task["id"], rect=cell).status_code == 200  # no key yet
        r = client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json={"answer": "28"})
        assert r.status_code == 200, r.text
        r = confirm_figure(client, task["id"], rect=cell)
        assert r.status_code == 422 and "28" in r.json()["error"]
        assert confirm_figure(client, task["id"], rect=cell, leak_ack=True).status_code == 200


def test_designations_and_unreadable_regions_and_the_size_of_the_answer_text():
    from app.ingest import figures
    from app.ingest.leaks import crop_units
    from app.ingest.schema import InvalidStructure, validate_tasks

    # A digits-only option is a label on the figure, not an answer text.
    assert (
        crop_units(None, [], [{"label": "А", "text": "1, 2"}, {"label": "Б", "text": "3-4"}]) == []
    )
    assert crop_units(None, [], [{"label": "А", "text": "ядро"}]) == ["ядро"]
    assert crop_units("ядро", ["ядро", "Б"], None) == ["ядро"]

    doc = pymupdf.open()
    try:
        tiny = doc.new_page()
        tiny.insert_text((70, 100), "1 2 3", fontsize=2, fontname="helv")
        big = doc.new_page()
        big.insert_text((70, 100), "1 2 3", fontsize=14, fontname="helv")
        area = [50, 80, 200, 120]
        assert figures.regions_readable(doc, [{"page": 2, "rect": area}])
        assert not figures.regions_readable(doc, [{"page": 1, "rect": area}])
        both = [{"page": 2, "rect": area}, {"page": 1, "rect": area}]
        assert not figures.regions_readable(doc, both)
    finally:
        doc.close()

    long = lt("5", "Сколько?", "numeric", answer_text="1" * 4001)
    try:
        validate_tasks({"tasks": [long]}, {1})
    except InvalidStructure:
        pass
    else:
        raise AssertionError("an answer_text over the limit was accepted")


def test_what_makes_an_open_task_a_scored_answer():
    from app.ingest.importer import DRAWING_REASON, OPEN_REASON, SCORED_REASON, _classify

    def task(**kw):
        base = {"type": "open", "text": "Объясните.", "points": None, "answer_text": None}
        return {**base, "refers_to_figure": False, **kw}

    assert _classify(task()) == (OPEN_REASON, False)
    assert _classify(task(points=3)) == (SCORED_REASON, False)
    assert _classify(task(answer_text="Ответ (2 балла)")) == (SCORED_REASON, False)
    assert _classify(task(text="Нарисуйте клетку.", points=3)) == (DRAWING_REASON, False)
    assert _classify(task(type="numeric", text="Сколько?"))[0] is None
    assert _classify(task(type="numeric", text="Смотрите рисунок.")) == (
        _classify(task(type="single", text="Смотрите рисунок."))
    )
