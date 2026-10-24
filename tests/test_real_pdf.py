"""A local check on the owner's real collection; skipped unless OLYMP_REAL_PDF is set.

The file is read from the given path only. The import runs in a temporary directory that is
removed afterwards; nothing of the book is written into the repository, the CI or the hub, and
the test prints counts only.
"""

import os
import re
import tempfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.bank_helpers import CHILD, PARENT, StubModel, build_app, db, upload

REAL = os.environ.get("OLYMP_REAL_PDF")
pytestmark = pytest.mark.skipif(
    not REAL or not Path(REAL).is_file(), reason="OLYMP_REAL_PDF is not set"
)

# Answers of the pages 103-105 as they are given in the task (AC-1 of OH-T05b).
KNOWN_ANSWERS = [
    "1 Стебель",
    "2 Лист",
    "3 Корень",
    "4 Стебель",
    "5 Корень",
    "10 баллов",
    "28",
    "№ 2",
    "№ 6",
    "№ 1",
    "2 4 6",
]
FINAL_STAGE_FROM = 103
HEADING = re.compile(r"^(\d{1,2})\.(\d{1,3})\s")
PAGE_MARK = re.compile(r"\[\[стр\. (\d+)\]\]\n")


class HeadingSplitter:
    """Stands in for the model: a task starts at a «N.M» heading. A section starts when the
    numbering restarts or the stage changes. With `clean`, the known answers move from the
    condition into answer_text, as a correct splitter does; without it they stay in the text."""

    def __init__(self, clean: bool):
        self.clean = clean
        self.last = (0, 0)
        self.block = 1
        self.stage = "отборочный"
        self.emitted: list[dict] = []

    def _section(self, number: tuple[int, int], page: int) -> str:
        stage = "заключительный" if page >= FINAL_STAGE_FROM else "отборочный"
        if stage != self.stage or number <= self.last:
            self.block += 1
        self.stage, self.last = stage, number
        return f"{stage}, блок {self.block}"

    def _answers_in(self, text: str) -> list[str]:
        return [a for a in KNOWN_ANSWERS if re.search(rf"(?<![\w]){re.escape(a)}(?![\w])", text)]

    def __call__(self, content: str):
        body = content.split("\n", 2)[-1]
        parts = PAGE_MARK.split(body)
        lines: list[tuple[int, str]] = []
        for page, text in zip(parts[1::2], parts[2::2], strict=True):
            lines += [(int(page), line) for line in text.splitlines()]
        starts = [i for i, (_, line) in enumerate(lines) if HEADING.match(line)]
        tasks = []
        for at, begin in enumerate(starts):
            end = starts[at + 1] if at + 1 < len(starts) else len(lines)
            piece = lines[begin:end]
            found = HEADING.match(piece[0][1])
            number = (int(found.group(1)), int(found.group(2)))
            text = "\n".join(line for _, line in piece)[:6000]
            page = piece[0][0]
            answers = self._answers_in(text) if page >= FINAL_STAGE_FROM else []
            if self.clean:
                for answer in answers:
                    text = re.sub(rf"(?<![\w]){re.escape(answer)}(?![\w])", "", text)
            task = {
                "number": f"{number[0]}.{number[1]}",
                "type": "numeric",
                "condition": text,
                "pages": sorted({p for p, _ in piece}),
                "refers_to_figure": "[рисунок]" in text,
                "section": self._section(number, page),
            }
            if answers:
                task["answer_text"] = "\n".join(answers)
            tasks.append(task)
        self.emitted += tasks
        return {"tasks": tasks}


def run_import(tmp, clean: bool):
    splitter = HeadingSplitter(clean)
    guess = {"proposed_answer": "1", "explanation": "e", "uncertainty": "low"}
    model = StubModel(split=splitter, answer=lambda content: guess)
    client = TestClient(build_app(Path(tmp), model))
    client.__enter__()
    with open(REAL, "rb") as handle:
        resp = upload(client, handle.read(), name="collection.pdf")
    assert resp.status_code == 202, resp.text
    status = client.get(f"/parent/materials/{resp.json()['material_id']}", headers=PARENT).json()
    assert status["import_status"] == "done", status
    return client, splitter


def test_no_task_is_lost_and_the_first_figure_covers_all_its_photos():
    with tempfile.TemporaryDirectory() as tmp:
        client, splitter = run_import(tmp, clean=True)
        with client:
            keys = [(t["section"], t["number"]) for t in splitter.emitted]
            conn = db(Path(tmp))
            rows = conn.execute("SELECT * FROM task_item").fetchall()
            assert len(keys) >= 300
            assert len(rows) == len(set(keys))  # merged only when section AND number repeat
            sections = {r["section"] for r in rows}
            numbers = {r["source_number"] for r in rows}
            assert len(sections) > 5 and len(rows) > len(numbers)

            first = conn.execute(
                "SELECT * FROM task_item WHERE source_number = '1.1' AND page_from = 4"
            ).fetchone()
            assert first is not None
            figure = client.get(f"/parent/tasks/{first['id']}", headers=PARENT).json()["figure"]
            doc = pymupdf.open(REAL)
            try:
                photos = [
                    pymupdf.Rect(i["bbox"])
                    for i in doc[3].get_image_info()
                    if i["bbox"][2] - i["bbox"][0] > 20 and i["bbox"][3] - i["bbox"][1] > 20
                ]
            finally:
                doc.close()
            region = next(r for r in figure["regions"] if r["page"] == 4)
            frame = pymupdf.Rect(region["rect"])
            assert len(photos) >= 5
            assert all((frame + (-1, -1, 1, 1)).contains(p) for p in photos), (frame, photos)
            print(
                f"REAL-PDF counts: tasks={len(rows)} sections={len(sections)} "
                f"figures={sum(1 for r in rows if r['figure'])} photos_p4={len(photos)} "
                f"regions_of_1.1={len(figure['regions'])} leak_flags={len(figure['leak'])}"
            )


@pytest.mark.parametrize("clean", [True, False])
def test_the_child_never_sees_the_known_answers_of_pages_103_to_105(clean):
    with tempfile.TemporaryDirectory() as tmp:
        client, splitter = run_import(tmp, clean)
        with client:
            conn = db(Path(tmp))
            rows = conn.execute(
                "SELECT * FROM task_item WHERE page_from BETWEEN 103 AND 105 "
                "AND import_status = 'draft'"
            ).fetchall()
            assert rows
            confirmed, refused = 0, 0
            for row in rows:
                r = client.post(f"/parent/tasks/{row['id']}/confirm", headers=PARENT, json={})
                confirmed += r.status_code == 200
                refused += r.status_code == 422
            visible = [client.get(f"/api/tasks/{r['id']}", headers=CHILD) for r in rows]
            shown = [v.text for v in visible if v.status_code == 200]
            blob = "\n".join(shown)
            for answer in KNOWN_ANSWERS:
                pattern = rf"(?<![\w]){re.escape(answer)}(?![\w])"
                assert not re.search(pattern, blob), answer
            for word in ("Стебель", "Корень"):
                found = client.get("/api/search", params={"q": word}, headers=CHILD).json()
                assert not any("1 Стебель" in x["snippet"] for x in found["results"])
            with_answers = {
                (t["section"], t["number"]) for t in splitter.emitted if t.get("answer_text")
            }
            assert with_answers, "no known answer was found on pages 103-105"
            if clean:  # the tasks are really shown, and still without the answers
                assert shown and len(shown) == confirmed
            if not clean:  # answers left in the condition: the task is held back, not shown
                assert refused >= 1
                for row in rows:
                    if (row["section"], row["source_number"]) in with_answers:
                        assert (
                            client.get(f"/api/tasks/{row['id']}", headers=CHILD).status_code == 404
                        )
            print(
                f"REAL-PDF leak sweep clean={clean}: tasks={len(rows)} confirmed={confirmed} "
                f"refused={refused} visible={len(shown)}"
            )
