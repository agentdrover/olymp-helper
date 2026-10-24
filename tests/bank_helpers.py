"""Shared builders for the task-bank tests: PDFs built on the fly, a stub model, app factory."""

import hashlib
import hmac
import sqlite3
import threading
from dataclasses import dataclass, field

import pymupdf

from app.main import create_app
from app.model.types import ModelResult

TEST_PARENT_KEY = "test-parent-key"


def parent_cookie(key: str) -> str:
    """Independent of the app: what a correct parent cookie must be."""
    return hmac.new(key.encode(), b"parent", hashlib.sha256).hexdigest()


PARENT = {"Cookie": "olymp_parent=" + parent_cookie(TEST_PARENT_KEY)}
CHILD: dict = {}
FONT = pymupdf.Font("helv")

OPTS4 = [{"label": c, "text": f"вариант {c.lower()}"} for c in "АБВГ"]


# ---------------------------------------------------------------- PDF builders


def write_lines(page, lines, x=50, y=80, size=11, step=16):
    tw = pymupdf.TextWriter(page.rect)
    for i, line in enumerate(lines):
        tw.append((x, y + i * step), line, font=FONT, fontsize=size)
    tw.write_text(page)


def quad_png(w=120, h=90) -> bytes:
    """Four differently coloured quadrants: orientation and cropping are visible in pixels."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h))
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for i, col in enumerate(colors):
        x0, y0 = (i % 2) * w // 2, (i // 2) * h // 2
        pix.set_rect(pymupdf.IRect(x0, y0, x0 + w // 2, y0 + h // 2), col)
    return pix.tobytes("png")


def doc_bytes(doc) -> bytes:
    doc.subset_fonts()
    return doc.tobytes(garbage=3, deflate=True)


def text_pdf(pages: list[list[str]]) -> bytes:
    """Pages of text lines. A page with a very long text is set small and wrapped, so that it
    stays on the page: the extractor reads only what lies on the page."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        if sum(len(line) for line in lines) <= 2000:
            write_lines(page, lines)
            continue
        wrapped = [line[i : i + 200] for line in lines for i in range(0, len(line) or 1, 200)]
        write_lines(page, wrapped, size=4, step=5)
    return doc_bytes(doc)


def key_pdf(pairs: dict[str, str], title="Ключи") -> bytes:
    return text_pdf([[title] + [f"{n}. {a}" for n, a in pairs.items()]])


def figure_pdf(kind: str, task_y=400, labels_size=None, text=True) -> bytes:
    """One page with task 11 and a figure of the requested kind."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    img = quad_png()
    if kind == "raster":
        page.insert_image(pymupdf.Rect(60, 120, 260, 260), stream=img, keep_proportion=False)
    elif kind == "vector":
        page.draw_rect(pymupdf.Rect(60, 120, 260, 260), color=(0, 0, 0), fill=(0.8, 0.9, 1))
        page.draw_circle(pymupdf.Point(160, 190), 40, color=(1, 0, 0))
    elif kind == "scan":
        page.insert_image(page.rect, stream=img, keep_proportion=False)
    elif kind == "twice":
        page.insert_image(pymupdf.Rect(60, 60, 200, 150), stream=img, keep_proportion=False)
        page.insert_image(pymupdf.Rect(60, 470, 200, 560), stream=img, keep_proportion=False)
    elif kind == "rotated":
        page.insert_image(pymupdf.Rect(120, 140, 320, 280), stream=img, keep_proportion=False)
    if labels_size:
        page.insert_text((70, 135), "1 2 3", fontsize=labels_size, fontname="helv")
    if text:
        write_lines(
            page,
            ["11. Рассмотрите рисунок и выберите верный ответ."],
            x=60 if kind == "rotated" else 50,
            y=task_y,
        )
    if kind == "rotated":  # CropBox and rotation last: everything above is in plain page space
        page.set_cropbox(pymupdf.Rect(50, 60, 450, 700))
        page.set_rotation(90)
    return doc_bytes(doc)


# ----------------------------------------------------------------- model stub


def make_task(
    number,
    text="Что изучает цитология?",
    type="single",
    pages=(1,),
    figure=False,
    options=None,
    section=None,
    variant=None,
    stage=None,
):
    task = {
        "number": str(number),
        "type": type,
        "text": text,
        "options": None if type == "open" else (OPTS4 if options is None else options),
        "pages": list(pages),
        "refers_to_figure": figure,
    }
    for name, value in (("section", section), ("variant", variant), ("stage", stage)):
        if value is not None:
            task[name] = value
    return task


def reply(*tasks) -> dict:
    return {"tasks": list(tasks)}


@dataclass
class Call:
    system: str
    messages: list
    schema: dict


@dataclass
class StubModel:
    """Replaces only the model's answer. split(content) -> dict; answer(content) -> dict | None."""

    split: object = None
    answer: object = None
    calls: list = field(default_factory=list)

    def complete(self, system, messages, schema=None, session_id=None):
        self.calls.append(Call(system, messages, schema))
        content = messages[-1]["content"]
        kind = (schema or {}).get("title")
        handler = self.split if kind == "tasks_split" else self.answer
        payload = handler(content) if callable(handler) else handler
        if payload is None:
            return ModelResult.failure("claude", "stub", "error")
        return ModelResult(None, payload, "claude", "stub", 1, None)

    def calls_of(self, title):
        return [c for c in self.calls if (c.schema or {}).get("title") == title]


def answer_by_number(mapping: dict):
    """Answer stub: the proposed answer depends on the task number found in the data block."""

    def handler(content):
        for number, ans in mapping.items():
            if f"№{number}\n" in content:
                if ans is None:
                    return None
                return {"proposed_answer": ans, "explanation": "e", "uncertainty": "low"}
        return None

    return handler


class BlockingModel:
    """A model that does not answer until released (hung import); then it answers fine."""

    def __init__(self, text="Что изучает цитология?"):
        self.text = text
        self.release = threading.Event()
        self.entered = threading.Event()

    def complete(self, system, messages, schema=None, session_id=None):
        self.entered.set()
        self.release.wait(30)
        return ModelResult(None, reply(make_task(1, self.text)), "claude", "stub", 1, None)


# ------------------------------------------------------------------ app helpers


def build_app(tmp_path, model=None, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    kw.setdefault("sync_imports", True)
    return create_app(db_path=tmp_path / "olymp.db", model=model, **kw)


def upload(
    client,
    pdf,
    kind="tasks",
    variant="1",
    year="2022",
    stage="отборочный",
    name="f.pdf",
    olympiad="sechenov",
):
    return client.post(
        "/parent/materials",
        headers=PARENT,
        files={"file": (name, pdf, "application/pdf")},
        data={
            "kind": kind,
            "variant": variant,
            "year": year,
            "stage": stage,
            "olympiad": olympiad,
        },
    )


def db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "olymp.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def task_ids(tmp_path):
    conn = db(tmp_path)
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM task_item ORDER BY id")]
    finally:
        conn.close()


def row(tmp_path, task_id):
    conn = db(tmp_path)
    try:
        return conn.execute("SELECT * FROM task_item WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()


def raw_pdf(stream_entries: str, data: bytes, extra: bytes = b"") -> bytes:
    """A PDF with one extra stream object whose dictionary is written as given; `extra` is
    raw text of further objects (5 0 obj ...)."""
    head = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
    )
    obj = b"4 0 obj<</Length %d %s>>stream\n" % (len(data), stream_entries.encode())
    return head + extra + obj + data + b"\nendstream endobj\ntrailer<</Root 1 0 R/Size 9>>\n%%EOF"
