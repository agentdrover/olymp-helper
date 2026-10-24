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
from tests.layout_pdf import (
    IMG as IMAGE,
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


def import_layout(tmp_path, tasks=None, answer=None, tiny_label=False, pdf=None):
    model = StubModel(split=reply(*(tasks or good_tasks())), answer=answer)
    app = build_app(tmp_path, model)
    client = TestClient(app)
    client.__enter__()
    resp = upload(client, pdf or layout_pdf(tiny_label))
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
        assert d["key_candidate"]["answer"] == "28" and d["answer_text"] == "28"
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
        # A manual frame edits the region of its page only; the other page stays.
        assert [r["page"] for r in decided["regions"]] == [1, 2]
        assert near(decided["regions"][0]["rect"], table)
        assert near(decided["rect"], table) and decided["page"] == 1
        assert near(decided["regions"][1]["rect"], union(PHOTOS_P2))
        assert detail(client, t11["id"])["figure"]["confirmed"]
        stacked = pymupdf.Pixmap((tmp_path / decided["file"]).read_bytes())
        assert stacked.height >= 2 * (210 + union(PHOTOS_P2).height) - 3
        # The frame of the second page is edited without touching the first.
        r = confirm_figure(client, t11["id"], rect=[60, 55, 190, 120], page=2, leak_ack=True)
        assert r.status_code == 200, r.text
        again = detail(client, t11["id"])["figure"]["regions"]
        assert near(again[0]["rect"], table) and near(again[1]["rect"], [60, 55, 190, 120])
        r = confirm_figure(client, t11["id"], rect=[60, 55, 190, 120], page=3, leak_ack=True)
        assert r.status_code == 409, r.text

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
        assert crop_leaks(doc, [{"page": 3, "rect": [0, 0, 595, 842]}], ["", " "]) == []
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
    assert crop_units("ядро", ["ядро", "Б"], None) == ["ядро", "б"]
    assert crop_units(None, ["Б", "7"], None) == ["б", "7"]  # a short key is looked for as a token
    assert crop_units("Ответ 7", [], None) == ["ответ 7", "7"]

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


def test_a_task_without_a_next_task_takes_the_images_up_to_its_last_page(tmp_path):
    from app.ingest import figures

    only = good_tasks()[:1]
    with import_layout(tmp_path, only) as client:
        figure = detail(client, by_number(tmp_path, "1.1")["id"])["figure"]
        assert [r["page"] for r in figure["regions"]] == [1, 2]
        assert near(figure["regions"][1]["rect"], union([*PHOTOS_P2, *CAPTION_PHOTOS]))
    doc = pymupdf.open("pdf", layout_pdf())
    try:
        assert [r["page"] for r in figures.suggest_regions(doc, 1, 95.0, 2)] == [1, 2]
        assert [r["page"] for r in figures.suggest_regions(doc, 1, 95.0, 1)] == [1]
    finally:
        doc.close()


def test_the_stacked_picture_holds_every_region_in_its_own_place():
    from app.ingest import figures

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        regions = [
            {"page": 1, "rect": list(union(PHOTOS_P1))},
            {"page": 2, "rect": list(union(PHOTOS_P2))},
        ]
        stacked = pymupdf.Pixmap(figures.render_regions_png(doc, regions))
        parts = [
            pymupdf.Pixmap(figures.render_figure_png(doc, r["page"], r["rect"])) for r in regions
        ]
        assert stacked.height == sum(p.height for p in parts)
        top = 0
        for part in parts:
            for x, y in (
                (8, 8),
                (part.width - 9, 8),
                (8, part.height - 9),
                (part.width // 2, part.height // 2),
            ):
                assert tuple(stacked.pixel(x, top + y)) == tuple(part.pixel(x, y)), (top, x, y)
            top += part.height
    finally:
        doc.close()


def test_the_crop_guard_looks_at_every_region_and_at_parts_of_a_line():
    from app.ingest.figures import crop_leaks

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        clean, dirty = (
            {"page": 2, "rect": [60, 560, 70, 570]},
            {"page": 2, "rect": [50, 510, 400, 536]},
        )
        assert crop_leaks(doc, [clean, dirty], ["28"]) == ["28"]
        assert crop_leaks(doc, [clean], ["28"]) == []
        # The line holds the unit (and more): the unit is printed in the frame.
        row = [{"page": 3, "rect": [50, 275, 300, 301]}]
        assert crop_leaks(doc, row, ["стебель"]) == ["стебель"]
    finally:
        doc.close()
    # A one-character line of the frame is not a part of the answer text.
    sheet = pymupdf.open()
    try:
        sheet.new_page().insert_text((70, 100), "1", fontsize=12, fontname="helv")
        frame = [{"page": 1, "rect": [50, 80, 200, 120]}]
        assert crop_leaks(sheet, frame, ["1 стебель 2 лист"]) == []
        assert crop_leaks(sheet, frame, ["1"]) == ["1"]  # a short answer as a whole token
    finally:
        sheet.close()


def test_units_of_the_answer_text_and_the_fitting_of_a_page_answer():
    from app.ingest.answers import fit_page_answer, same_answer
    from app.ingest.leaks import condition_leaks, crop_units

    assert condition_leaks("Подсчитайте. 28", "28\n№ 2 … № 7") == ["28"]
    assert condition_leaks("Подсчитайте.", "28\n№ 2 … № 7") == []
    assert crop_units("ядро", [], None) == ["ядро"]
    assert crop_units(None, ["ядро"], None) == ["ядро"]
    assert not same_answer("numeric", None, "x")
    assert fit_page_answer("А", "open", [{"label": "А", "text": "один"}]) is None
    assert fit_page_answer("1 Стебель 2 Лист", "match", MATCH_6) is None  # not every left label
    assert fit_page_answer("1 Стебель 2 Лист 3 Корень 4 Лист 5 Корень", "match", MATCH_6) == {
        "1": "Б",
        "2": "В",
        "3": "А",
        "4": "В",
        "5": "А",
    }


def test_an_unreadable_label_in_the_second_region_makes_the_task_unsupported(tmp_path):
    with import_layout(tmp_path, tiny_label=True) as client:
        task = by_number(tmp_path, "1.1")
        figure = detail(client, task["id"])["figure"]
        assert [r["page"] for r in figure["regions"]] == [1, 2]
        r = confirm_figure(client, task["id"])
        assert r.status_code == 200 and r.json()["status"] == "unsupported", r.text


def _numeric_figure_tasks(answer_text):
    tasks = good_tasks()
    tasks[2] = lt(
        "1.3",
        "Сколько хромосом содержит соматическая клетка? Смотрите таблицу.",
        "numeric",
        (2,),
        answer_text=answer_text,
        refers_to_figure=True,
    )
    return tasks


def test_the_candidate_from_an_uploaded_key_is_checked_against_the_frame(tmp_path):
    from tests.bank_helpers import key_pdf

    with import_layout(tmp_path, _numeric_figure_tasks(None)) as client:
        assert upload(client, key_pdf({"1.3": "28"}), kind="key", name="k.pdf").status_code == 202
        task = by_number(tmp_path, "1.3")
        r = confirm_figure(client, task["id"], rect=[150, 510, 400, 536])
        assert r.status_code == 422 and "28" in r.json()["error"]


def test_the_answer_text_is_checked_against_the_frame_even_without_a_candidate(tmp_path):
    with import_layout(tmp_path, _numeric_figure_tasks("28 хромосом")) as client:
        task = by_number(tmp_path, "1.3")
        assert detail(client, task["id"])["key_candidate"] is None
        r = confirm_figure(client, task["id"], rect=[150, 510, 400, 536])
        assert r.status_code == 422 and "28" in r.json()["error"]


def test_short_answers_are_looked_for_by_context_in_the_condition():
    from app.ingest.leaks import condition_leaks

    def leaks(condition, answer, kind="single"):
        return bool(condition_leaks(condition, answer, kind))

    assert leaks("Подсчитайте. Ответ: 7", "7", "numeric")
    assert leaks("Подсчитайте. ответ 7", "7")
    assert leaks("Какой? Ответ - Б", "Б")
    assert leaks("Какой? 2+5 = 7", "7")
    assert leaks("Какой? 2+5 → 7", "7")
    assert leaks("Выберите верное | Б", "Б")  # alone in a table cell
    assert leaks("Выберите верное.\nБ", "Б")  # alone in the last line
    assert leaks("Выберите верное.\nБ.", "Б")
    assert leaks("У паука 7 глаз.", "7", "numeric")  # a number of a numeric task, anywhere
    assert leaks("У паука 7,0 глаз.", "7", "numeric")
    assert not leaks("У паука 17 глаз.", "7", "numeric")
    assert not leaks("У паука 7 глаз.", "7", "single")  # only a numeric task bars the number
    assert not leaks("Среди 7 видов выберите верное.", "Б")  # an ordinary number
    assert not leaks("Выберите верное. Б) ядро и А) клетка", "Б")
    assert not leaks("Ответ запишите числом.", "7", "numeric")
    assert not leaks("Выберите верное.\nБ\nВыберите ещё.", "Б")  # not the last line
    assert not leaks("Ответ: 8", "7")


def test_a_short_answer_left_in_the_condition_is_not_published(tmp_path):
    tasks = good_tasks()
    tasks[2] = lt("1.3", "Подсчитайте хромосомы. Ответ: 7", "numeric", (2,), "7")
    with import_layout(tmp_path, tasks) as client:
        task = by_number(tmp_path, "1.3")
        assert detail(client, task["id"])["publication"]["blocked"]
        assert confirm(client, task["id"]).status_code == 422
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404
        assert not client.get("/api/search", params={"q": "хромосомы"}, headers=CHILD).json()[
            "results"
        ]

    # A number of a numeric task is barred anywhere in the condition.
    tasks[2] = lt("1.3", "У паука 7 глаз. Подсчитайте.", "numeric", (2,), "7")
    with import_layout(tmp_path / "n", tasks) as client:
        task = by_number(tmp_path / "n", "1.3")
        assert detail(client, task["id"])["publication"]["blocked"]
        assert confirm(client, task["id"]).status_code == 422


def _cell_figure_task(tmp_path, tasks=None):
    cell = [150, 510, 400, 536]  # the cell with 28
    client = import_layout(tmp_path, tasks or _numeric_figure_tasks(None))
    task = by_number(tmp_path, "1.3")
    assert confirm_figure(client, task["id"], rect=cell).status_code == 200  # no key yet
    return client, task, cell


def test_a_key_added_after_the_figure_is_confirmed_reruns_the_crop_guard(tmp_path):
    client, task, cell = _cell_figure_task(tmp_path)
    with client:
        r = client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json={"answer": "28"})
        assert r.status_code == 200, r.text
        d = detail(client, task["id"])
        assert d["status"] == "unsupported" and not d["figure"]["confirmed"]
        assert "28" in d["figure"]["leak"] and d["figure"]["leak_ack"] is False
        r = confirm(client, task["id"])
        assert r.status_code == 422, r.text
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404
        assert confirm_figure(client, task["id"], rect=cell).status_code == 422
        assert confirm_figure(client, task["id"], rect=cell, leak_ack=True).status_code == 200
        assert confirm(client, task["id"]).status_code == 200
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 200
        # Another key resets the acknowledgement: it was given for the other key.
        r = client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json={"answer": "29"})
        assert r.status_code == 200
        fig = detail(client, task["id"])["figure"]
        assert fig["leak_ack"] is False and fig["leak"] == []


def test_a_candidate_confirmed_after_the_figure_reruns_the_crop_guard(tmp_path):
    from tests.bank_helpers import key_pdf

    client, task, _cell = _cell_figure_task(tmp_path)
    with client:
        assert upload(client, key_pdf({"1.3": "28"}), kind="key", name="k.pdf").status_code == 202
        r = confirm(client, task["id"])
        assert r.status_code == 422, r.text
        d = detail(client, task["id"])
        assert d["status"] == "unsupported" and "28" in d["figure"]["leak"]
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404


def test_a_numeric_key_is_compared_as_a_string_without_rounding():
    from app.ingest.answers import normalize_answer, same_answer

    a, b = "123456789012345678901234567890", "123456789012345678901234567891"
    assert not same_answer("numeric", normalize_answer(a, "numeric"), b)
    assert normalize_answer(a, "numeric") == a
    assert normalize_answer("0." + "0" * 40 + "1", "numeric") == "0." + "0" * 40 + "1"
    for given in ("28", " 28 ", "28,0", "028", "28.", "+28", "0028,000"):
        assert same_answer("numeric", "28", given), given
    assert normalize_answer("-0", "numeric") == "0" and normalize_answer("-0,0", "numeric") == "0"
    assert normalize_answer("-028,50", "numeric") == "-28.5"
    assert (
        normalize_answer(".5", "numeric") is None and normalize_answer("1 000", "numeric") is None
    )
    assert normalize_answer("1" * 200, "numeric") is None


def test_readability_is_judged_at_the_scale_of_the_stacked_picture():
    from app.ingest import figures

    doc = pymupdf.open()
    try:
        doc.new_page().insert_text((55, 90), "1 2", fontsize=4, fontname="helv")
        doc.new_page()
        regions = [
            {"page": 1, "rect": [50, 70, 150, 110]},  # 100 pt wide, a 4 pt label
            {"page": 2, "rect": [50, 70, 550, 110]},  # 500 pt wide
        ]
        assert figures.regions_readable(doc, regions[:1])  # alone it is shown 5.6 times larger
        assert not figures.regions_readable(doc, regions)  # stacked: 560 / 500 = 1.12 times
    finally:
        doc.close()


def test_a_key_added_after_the_figure_reruns_the_alt_check(tmp_path):
    clean = [60, 600, 120, 620]  # no number in it
    client = import_layout(tmp_path, _numeric_figure_tasks(None))
    with client:
        task = by_number(tmp_path, "1.3")
        r = confirm_figure(client, task["id"], rect=clean, alt="На рисунке 28 хромосом")
        assert r.status_code == 200, r.text  # no key yet: nothing to compare with
        key = {"answer": "28"}
        assert (
            client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json=key).status_code
            == 200
        )
        d = detail(client, task["id"])
        assert d["status"] == "unsupported" and not d["figure"]["confirmed"]
        assert d["figure"]["leak_ack"] is False
        assert confirm(client, task["id"]).status_code == 422
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 404
        # The alt is rewritten without the key: the figure and the task go through.
        assert (
            confirm_figure(client, task["id"], rect=clean, alt="Фотографии клетки").status_code
            == 200
        )
        assert confirm(client, task["id"]).status_code == 200
        assert client.get(f"/api/tasks/{task['id']}", headers=CHILD).status_code == 200


def test_an_alt_without_the_new_key_keeps_the_figure_confirmed(tmp_path):
    client = import_layout(tmp_path, _numeric_figure_tasks(None))
    with client:
        task = by_number(tmp_path, "1.3")
        r = confirm_figure(client, task["id"], rect=[60, 600, 120, 620], alt="Фотографии клетки")
        assert r.status_code == 200
        key = {"answer": "28"}
        assert (
            client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json=key).status_code
            == 200
        )
        d = detail(client, task["id"])
        assert d["figure"]["confirmed"] and d["status"] != "unsupported"


def test_the_designations_of_a_match_task_are_not_short_answers():
    from app.ingest.leaks import condition_leaks, crop_units

    def leaks(condition, answer="1-А; 2-Б", kind="match"):
        return bool(condition_leaks(condition, answer, kind))

    rows = "Описания органов:\n1 | Корень поглощает воду\n2 | Лист осуществляет фотосинтез"
    assert not leaks(rows)
    assert not leaks("Соотнесите 1 и 2 с органами А и Б.\nБ")
    assert leaks(rows + "\n1 — А | 2 — Б")
    assert leaks("Соотнесите. 1-А")
    assert leaks("Соотнесите. 2Б")
    assert leaks("Соотнесите: 1 — А")
    assert not leaks("Соотнесите: 1 — Аист")
    assert leaks("Соотнесите.\nОтвет: 1-А; 2-Б")
    # A choice answer is a label by nature: it stays checked by context.
    assert leaks("Выберите.\nБ", "Б", "single")
    # In the frame, the pairs are looked for, not the bare designations.
    assert crop_units("1-А; 2-Б", [], None, "match") == ["1 а 2 б", "1 а", "2 б"]
    assert crop_units("1-А; 2-Б", [], None, "single") == [
        "1 а 2 б",
        "1 а",
        "2 б",
        "1",
        "а",
        "2",
        "б",
    ]


def test_pairs_are_the_answer_of_a_match_only_and_come_in_both_spellings():
    from app.ingest.leaks import condition_leaks

    assert condition_leaks("Соотнесите: 1А и 2Б", "1А; 2Б", "match")
    assert condition_leaks("Соотнесите: 1-А", "1 А", "match")
    assert condition_leaks("Соотнесите: 1 — А", "1А; 2Б", "match")  # answer written as one word
    assert not condition_leaks("Соотнесите: 1А", "1-А", "multiple")
    assert condition_leaks("Соотнесите: 1А", "1-А", "match")


def test_a_match_figure_may_show_its_designations_but_not_its_pairs(tmp_path):
    from app.ingest.importer import _figure_record

    options = {
        "left": [{"label": "1", "text": "Фото 1"}, {"label": "2", "text": "Фото 2"}],
        "right": [{"label": "А", "text": "Корень"}, {"label": "Б", "text": "Стебель"}],
    }
    tasks = good_tasks()
    tasks[5] = lt(
        "1.6", "Соотнесите по рисунку.", "match", (3,), "1-А; 2-Б", options, refers_to_figure=True
    )
    with import_layout(tmp_path, tasks) as client:
        task = by_number(tmp_path, "1.6")
        assert detail(client, task["id"])["key_candidate"]["answer"]
        events = [50, 78, 300, 200]  # lines «№ 1. …» «№ 2. …» hold the numbers 1 and 2
        labels = [320, 80, 400, 100]  # «А Б»
        for frame in (events, labels):
            r = confirm_figure(client, task["id"], rect=frame, page=3)
            assert r.status_code == 200, (frame, r.text)

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(60, 100, 200, 160), stream=IMAGE, keep_proportion=False)
        page.insert_text((70, 130), "1", fontsize=12, fontname="helv")
        page.insert_text((60, 90), "Соотнесите фотографии", fontsize=11, fontname="helv")
        task = {
            "number": "1",
            "text": "Соотнесите фотографии",
            "pages": [1],
            "type": "match",
            "answer_text": "1-А",
            "options": None,
            "refers_to_figure": True,
        }
        assert _figure_record(doc, 1, task, None)["leak"] == []
        task["type"] = "single"  # the bare «1» is a short answer there
        assert _figure_record(doc, 1, task, None)["leak"] == ["1"]
    finally:
        doc.close()


def test_a_range_in_the_page_answer_is_expanded_to_every_label():
    from app.ingest.answers import fit_page_answer

    labels = [{"label": str(i), "text": f"утверждение {i}"} for i in range(1, 9)]
    every = ["2", "3", "4", "5", "6", "7"]
    for raw in ("№ 2 … № 7", "2-7", "2...7", "2 – 7", "2, 3, 4, 5, 6, 7", "2 … 7, 3"):
        assert fit_page_answer(raw, "multiple", labels) == every, raw
    assert fit_page_answer("1, 3 … 5", "multiple", labels) == ["1", "3", "4", "5"]
    # Ambiguous: an end is not a label, the range runs backwards, or its ends differ in kind.
    for raw in ("2 … 12", "7 … 2", "1, 7 … 2", "А … 3", "№ 2 … №"):
        assert fit_page_answer(raw, "multiple", labels) is None, raw
    assert fit_page_answer("2-7", "single", labels) is None
    letters = [{"label": c, "text": f"вариант {c}"} for c in "АБВГД"]
    assert fit_page_answer("А-В", "multiple", letters) == ["А", "Б", "В"]
    assert fit_page_answer("Б … Д", "multiple", letters) == ["Б", "В", "Г", "Д"]


def test_the_next_anchor_is_the_nearest_following_one_by_position(tmp_path):
    tasks = good_tasks()
    tasks[1], tasks[2] = tasks[2], tasks[1]  # emitted as 1.1, 1.3, 1.2
    with import_layout(tmp_path, tasks) as client:
        figure = detail(client, by_number(tmp_path, "1.1")["id"])["figure"]
        assert [r["page"] for r in figure["regions"]] == [1, 2]
        assert near(figure["regions"][1]["rect"], union(PHOTOS_P2))  # stops at 1.2, not at 1.3


def test_the_alt_check_compares_numbers_not_spellings():
    from app.ingest.figures import alt_problem

    for alt in ("Всего 28,5 мм", "Всего 28.50 мм", "Размер 28.5.", "(28,5)", "028,50"):
        assert alt_problem(alt, None, ["28.5"]), alt
    assert alt_problem("Размер 28.5 мм", None, ["28,50"])  # the key may be spelled either way
    assert alt_problem("Размер 285 мм", None, ["28.5"]) is None
    assert alt_problem("Размер 28,55 мм", None, ["28.5"]) is None
    assert alt_problem("Рисунок 12", None, ["Б"]) is None


def test_a_figure_stored_without_regions_is_rechecked_after_a_key_change(tmp_path):
    client, task, _cell = _cell_figure_task(tmp_path)
    with client:
        conn = db(tmp_path)
        figure = json.loads(
            conn.execute("SELECT figure FROM task_item WHERE id = ?", (task["id"],)).fetchone()[0]
        )
        for name in ("regions", "leak", "leak_ack"):
            figure.pop(name)  # the form written before regions existed
        assert figure["page"] and figure["rect"]
        conn.execute(
            "UPDATE task_item SET figure = ? WHERE id = ?", (json.dumps(figure), task["id"])
        )
        conn.close()
        r = client.post(f"/parent/tasks/{task['id']}/key", headers=PARENT, json={"answer": "28"})
        assert r.status_code == 200, r.text
        d = detail(client, task["id"])
        assert d["status"] == "unsupported" and not d["figure"]["confirmed"]
        assert "28" in d["figure"]["leak"]
        # Confirming it again reads the old record as one region and keeps the page.
        assert confirm_figure(client, task["id"], leak_ack=True).status_code == 200


def _same_prefix_pdf():
    from tests.bank_helpers import doc_bytes
    from tests.layout_pdf import _text

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for top, name in ((100, "первое"), (300, "второе")):
        _text(page, 50, top, f"Рассмотрите внимательно препарат, {name} задание.")
        page.insert_image(
            pymupdf.Rect(60, top + 10, 150, top + 70), stream=IMAGE, keep_proportion=False
        )
    page.insert_image(pymupdf.Rect(60, 180, 150, 240), stream=IMAGE, keep_proportion=False)
    return doc_bytes(doc)


def test_the_alt_check_finds_numbers_glued_to_words():
    from app.ingest.figures import alt_problem

    for alt in ("28,5мм", "Длина 2,5см и 28,5мм", "рис.28,5", "x28.5y", "(28,5)", "1-28,5"):
        assert alt_problem(alt, None, ["28.5"]), alt
    for alt in ("285", "28,55", "128,5"):
        assert alt_problem(alt, None, ["28.5"]) is None, alt


def _photos_pdf(photo_tops, next_page_y=None, same_start=False):
    """Page 1 holds the task and its photos; page 2 starts the next task at next_page_y."""
    from tests.bank_helpers import doc_bytes
    from tests.layout_pdf import _text

    doc = pymupdf.open()
    one = doc.new_page(width=595, height=842)
    _text(one, 50, 60, "Рассмотрите внимательно препарат и ответьте на вопрос.")
    for top in photo_tops:
        one.insert_image(pymupdf.Rect(60, top, 150, top + 50), stream=IMAGE, keep_proportion=False)
    two = doc.new_page(width=595, height=842)
    if next_page_y is not None:
        name = "Рассмотрите внимательно препарат и ответьте на вопрос." if same_start else "Другое"
        _text(two, 50, next_page_y, name if same_start else "Определите орган по описанию.")
    return doc_bytes(doc)


def _regions(pdf, texts, pages_per_task):
    from app.ingest.importer import _figure_record, _following, _positions

    doc = pymupdf.open("pdf", pdf)
    try:
        tasks = [
            {
                "text": t,
                "pages": pg,
                "number": str(i),
                "options": None,
                "type": "single",
                "refers_to_figure": True,
            }
            for i, (t, pg) in enumerate(zip(texts, pages_per_task, strict=True))
        ]
        pos = _positions(figures_index(doc), tasks)
        return [
            _figure_record(doc, 1, t, pos[i], _following(pos, i))["regions"]
            for i, t in enumerate(tasks)
        ], pos
    finally:
        doc.close()


def figures_index(doc):
    from app.ingest.figures import PageIndex

    return PageIndex(doc)


FIRST = "Рассмотрите внимательно препарат и ответьте на вопрос."
SECOND = "Определите орган по описанию."


def test_photos_before_the_next_page_anchor_are_all_taken():
    # Three photos on page 1; the next task starts on page 2 at y = 203: the y is the other
    # page's, so it must not cut page 1.
    for tops, expected in (((100, 170, 240), 3), ((100, 170, 240, 310), 4)):
        got, _ = _regions(_photos_pdf(tops, 203), [FIRST, SECOND], [[1], [2]])
        assert [r["page"] for r in got[0]] == [1]
        assert near(got[0][0]["rect"], [60, tops[0], 150, tops[-1] + 50]), (expected, got[0])


def test_the_stop_cuts_only_on_its_own_page_and_the_page_is_clamped():
    from app.ingest import figures

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        # The stop lies on page 2 and the task ends on page 1: no cut, bottom of page 1.
        got = figures.suggest_regions(doc, 1, 95.0, 1, stop=(2, 10.0))
        assert [r["page"] for r in got] == [1]
        got = figures.suggest_regions(doc, 1, 95.0, 1, stop=(2, 200.0))  # images above that y
        assert [r["page"] for r in got] == [1] and near(got[0]["rect"], union(PHOTOS_P1))
        # The stop lies on a page of the range: cut at its y on that page only.
        got = figures.suggest_regions(doc, 1, 95.0, 2, stop=(2, 200.0))
        assert [r["page"] for r in got] == [1, 2]
        assert near(got[1]["rect"], union(PHOTOS_P2))
        assert near(got[0]["rect"], union(PHOTOS_P1))
        # A stop above the images of its page leaves that page without a region.
        got = figures.suggest_regions(doc, 1, 95.0, 2, stop=(2, 30.0))
        assert [r["page"] for r in got] == [1]
        # No start y: no bounds, the nearest image only.
        nearest = figures.suggest_regions(doc, 2, None, 2)
        assert len(nearest) == 1 and near(nearest[0]["rect"], PHOTOS_P2[0])
        assert figures.suggest_regions(doc, 4, 100.0, 4) == []
    finally:
        doc.close()


def test_the_bottom_task_named_first_keeps_both_of_its_photos(tmp_path):
    tasks = good_tasks()
    tasks[1], tasks[2], tasks[0] = tasks[2], tasks[0], tasks[1]  # 1.3 numeric, 1.1, 1.2
    with import_layout(tmp_path, tasks) as client:
        one = detail(client, by_number(tmp_path, "1.1")["id"])["figure"]["regions"]
        two = detail(client, by_number(tmp_path, "1.2")["id"])["figure"]["regions"]
        assert [r["page"] for r in one] == [1, 2] and near(one[1]["rect"], union(PHOTOS_P2))
        assert [r["page"] for r in two] == [2] and near(two[0]["rect"], union(CAPTION_PHOTOS))


def test_identical_conditions_get_no_automatic_suggestion_in_either_order(tmp_path):
    def task(number):
        return lt(number, PREAMBLE, "single", (1,), "Б", OPTIONS_1_2, refers_to_figure=True)

    pdf = _same_start_pdf(identical=True)
    for order in (("1", "2"), ("2", "1")):
        with import_layout(tmp_path / "".join(order), [task(n) for n in order], pdf=pdf) as client:
            for number in order:
                d = detail(client, by_number(tmp_path / "".join(order), number)["id"])
                assert d["figure"]["regions"] == [] and d["figure"]["suggested"] is False
                assert d["figure"]["ambiguous"] is True
                assert d["card"]["kind"] == "needs_figure"
                assert "вручную" in d["card"]["hint"] and "повторяется" in d["card"]["hint"]
            # The parent draws the frame by hand: it works.
            one = by_number(tmp_path / "".join(order), order[0])
            r = confirm_figure(client, one["id"], rect=[60, 110, 150, 170], page=1)
            assert r.status_code == 200, r.text


PREAMBLE = "Рассмотрите внимательно препарат и ответьте на вопрос."


def test_conditions_that_share_a_long_start_are_each_found_at_their_own_place(tmp_path):
    def task(number, name):
        return lt(
            number, f"{PREAMBLE} {name}", "single", (1,), "Б", OPTIONS_1_2, refers_to_figure=True
        )

    for order in (("1", "2"), ("2", "1")):
        tasks = [task(n, "Первое." if n == "1" else "Второе.") for n in order]
        where = tmp_path / "".join(order)
        with import_layout(where, tasks, pdf=_same_start_pdf()) as client:
            one = detail(client, by_number(where, "1")["id"])["figure"]
            two = detail(client, by_number(where, "2")["id"])["figure"]
            assert one["ambiguous"] is False and two["ambiguous"] is False
            assert [near(r["rect"], [60, 110, 150, 240]) for r in one["regions"]] == [True]
            assert [near(r["rect"], [60, 310, 150, 370]) for r in two["regions"]] == [True]


def _same_start_pdf(identical=False):
    from tests.bank_helpers import doc_bytes
    from tests.layout_pdf import _text

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for top, name in ((100, "Первое."), (300, "Второе.")):
        tail = "" if identical else name
        _text(page, 50, top, "Рассмотрите внимательно препарат и ответьте на вопрос. " + tail)
        page.insert_image(
            pymupdf.Rect(60, top + 10, 150, top + 70), stream=IMAGE, keep_proportion=False
        )
    page.insert_image(pymupdf.Rect(60, 180, 150, 240), stream=IMAGE, keep_proportion=False)
    return doc_bytes(doc)


def test_the_page_index_counts_the_whole_material_and_normalises_the_start():
    from app.ingest.figures import AMBIGUOUS, PageIndex

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        index = PageIndex(doc)
        cell = "[рисунок] | Фото | ОПРЕДЕЛИТЕ   органоид, показанный на микрофотографии."
        found = index.find(cell)
        assert found[0] == 2 and 200 < found[1] < 230  # whitespace, case and cell markers
        assert index.find("[рисунок] | | ") is None  # nothing to look for
        assert index.find("Этого текста на странице нет") is None
        # The start of two different tasks: «1.» is on several pages, so it is not an anchor.
        assert index.find("1.3 Сколько хромосом содержит") != AMBIGUOUS
        assert index.find("Вариант 1. Отборочный этап") is not None
    finally:
        doc.close()
    doc = pymupdf.open("pdf", _same_start_pdf())
    try:
        index = PageIndex(doc)
        # The shortest start that is unique: a start shared by two places grows until it differs.
        assert index.find(PREAMBLE) == AMBIGUOUS  # even the whole condition is not unique
        assert index.find(PREAMBLE + " Первое.")[1] < 150
        assert index.find(PREAMBLE + " Второе.")[1] > 250
        assert index.find(PREAMBLE + " Третье.") == AMBIGUOUS  # grew into words nobody has
    finally:
        doc.close()


def test_the_following_anchor_is_the_next_one_in_page_order():
    from app.ingest.importer import _following

    pos = [(2, 5.0), (1, 10.0), None, "ambiguous", (1, 50.0)]
    assert _following(pos, 1) == (1, 50.0)  # whatever the model order
    assert _following(pos, 4) == (2, 5.0)
    assert _following(pos, 0) is None  # nothing known after it: no stop
    assert _following(pos, 2) is None and _following(pos, 3) is None  # no own anchor
    assert _following([(1, 10.0), (1, 10.0)], 0) is None  # equal is not after


def test_the_index_tiers_the_needle_and_places_it_at_the_first_word():
    from app.ingest.figures import AMBIGUOUS, PageIndex
    from tests.bank_helpers import doc_bytes
    from tests.layout_pdf import _text

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    _text(page, 50, 100, "Определите препарат под микроскопом, вариант А. Далее ответ.")
    _text(page, 50, 300, "Определите препарат под микроскопом, вариант Б. Далее ответ.")
    _text(page, 50, 500, "Ёж и ёлка растут в лесу, где много снега и ветра зимой")
    _text(page, 50, 600, "а б в")
    _text(page, 50, 640, "Рассмотрите внимательно препарат под лупой")
    _text(page, 50, 660, "Рассмотрите внимательно рисунок справа")
    _text(page, 50, 700, "Определите, пожалуйста, внимательно,")
    _text(page, 50, 716, "какой орган изображён на рисунке справа сейчас")
    _text(page, 50, 800, "и последнее слово")
    reopened = pymupdf.open("pdf", doc_bytes(doc))
    try:
        index = PageIndex(reopened)
        a = index.find("Определите препарат под микроскопом, вариант А. Далее ответ.")
        b = index.find("Определите препарат под микроскопом, вариант Б. Далее ответ.")
        assert a[1] < 120 and b[1] > 280  # the 60-character start tells them apart
        assert index.find("Определите препарат под микроскопом, вариант") == AMBIGUOUS
        # The first 60 characters are not on the page, the first 30 are: a shorter start.
        assert index.find("Ёж и елка растут в лесу, где совсем другой конец")[1] > 450
        assert index.find("Ёж и ёлка растут в лесу, где много снега, но дальше иначе")[1] > 450
        assert index.find("а  б  в") is None  # too short once normalised: not an anchor
        assert index.find("Х" * 100) is None
        # The start is unique at 30 characters even though the rest differs from the page.
        wrong = index.find("Рассмотрите внимательно препарат совсем другой конец")
        assert 620 < wrong[1] < 650
        # A shared short start with a wrong rest is not guessed.
        assert index.find("Рассмотрите внимательно совсем иначе тут") == AMBIGUOUS
        assert (
            index.find("слово другое далее тут") is None
        )  # the last word of a page  # a word longer than the needle: nothing to look for
        # The place is the first word, also when the start runs over a line.
        two = index.find("Определите, пожалуйста, внимательно, какой орган изображён на рисунке")
        assert 680 < two[1] < 700
    finally:
        reopened.close()


def test_the_figure_is_taken_where_the_anchor_is_not_where_the_model_says():
    from app.ingest.importer import _figure_record

    doc = pymupdf.open("pdf", layout_pdf())
    try:
        task = {
            "number": "1.1",
            "pages": [2],
            "options": None,
            "type": "single",
            "text": "x",
            "refers_to_figure": True,
            "answer_text": None,
        }
        record = _figure_record(doc, 1, task, (1, 95.0), (2, 200.0))
        assert [r["page"] for r in record["regions"]] == [1, 2]
        assert record["page"] == 1
    finally:
        doc.close()
