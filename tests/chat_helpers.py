"""Builders for the chat and voice tests (OH-T07): a textbook indexed straight into the search
tables, a scripted chat payload, a recognizer stub. No real model and no real whisper."""

import base64
import itertools
import re
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.search import index_pages
from app.voice.service import VoiceService
from tests.bank_helpers import CHILD, build_app, db, seed_state
from tests.tutor_helpers import Clock, Scripted, post

__all__ = [
    "FIXTURE_WEBM",
    "FakeFfmpeg",
    "Recognizer",
    "boot_chat",
    "chat_reply",
    "say",
    "textbook",
    "voice_post",
]

FIXTURE_WEBM = Path(__file__).parent / "fixtures" / "recording-chrome.webm"
_counter = itertools.count(1)


def textbook(tmp_path, pages: dict[int, str], kind="textbook"):
    """(material_id, {page: search id}). The search id is the FTS row id the server hands out."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    seed_state(tmp_path / "olymp.db")  # the schema exists before the app boots
    conn = db(tmp_path)
    try:
        n = next(_counter)
        mid = conn.execute(
            "INSERT INTO material (file, sha256, kind, year, import_status) "
            "VALUES (?, ?, ?, NULL, 'done')",
            (f"materials/book{n}.pdf", f"book-{n}", kind),
        ).lastrowid
        index_pages(conn, mid, sorted(pages.items()))
        ids = {
            page: conn.execute(
                "SELECT rowid FROM material_page WHERE material_id = ? AND page = ?", (mid, page)
            ).fetchone()[0]
            for page in pages
        }
        return mid, ids
    finally:
        conn.close()


def chat_reply(reply="Клетка - основа живого.", citations=(), suggestions=()):
    return {"reply": reply, "citations": list(citations), "suggestions": list(suggestions)}


def say(c, tid, message, mode="chat", **kw):
    return post(c, tid, "chat", message=message, mode=mode, **kw)


class FakeFfmpeg:
    """Stands in for the ffmpeg process: writes a silent mono 16 kHz wav of `seconds` and records
    the command and the stdin bytes. Only test_ac4 runs the real ffmpeg."""

    def __init__(self, seconds=2.0):
        self.seconds, self.cmds, self.inputs = seconds, [], []

    def __call__(self, cmd, timeout, data=None):
        self.cmds.append(list(cmd))
        self.inputs.append(data)
        with wave.open(cmd[-1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * int(16000 * self.seconds))
        return 0, ""


class Recognizer:
    """Stands in for whisper.cpp: gets the path of the converted wav, returns text."""

    def __init__(self, text="какой вариант верный", fn=None):
        self.text, self.fn, self.paths = text, fn, []

    def __call__(self, wav: Path, timeout: float) -> str:
        self.paths.append(Path(wav))
        return self.fn(wav, timeout) if self.fn else self.text


def voice_post(c, tid, data: bytes | None = None, mime="audio/webm;codecs=opus", **extra):
    data = FIXTURE_WEBM.read_bytes() if data is None else data
    body = {"audio": base64.b64encode(data).decode(), "mime": mime, **extra}
    return c.post(f"/tasks/{tid}/voice", json=body, headers=CHILD)


def boot_chat(tmp_path, model=None, recognizer=None, voice=None, notice=True):
    """(client, model, voice, audio dir). The caller uses the client as a context manager."""
    model = model if model is not None else Scripted(default=chat_reply())
    audio = tmp_path / "audio-work"
    audio.mkdir(parents=True, exist_ok=True)
    if voice is None:
        voice = VoiceService(recognizer or Recognizer(), workdir=audio, runner=FakeFfmpeg())
    clock = Clock()
    app = build_app(tmp_path, model, clock=clock, notice=notice, voice=voice)
    c = TestClient(app)
    c.clock = clock  # the tests move time
    c.__enter__()
    return c, model, voice, audio


def block_of(content: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", content, re.DOTALL)
    assert m, f"no <{tag}> block"
    return m.group(1)
