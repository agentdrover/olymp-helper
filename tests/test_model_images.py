"""Вложения PNG в канал модели и закреплённые семейства (#2112): AC-1, AC-2. PNG синтетические."""

import base64
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from app.db import connect, migrate
from app.model.client import ExecutorChannel
from app.model.executor_server import handle_request
from app.model.images import MAX_IMAGE_BYTES, MAX_IMAGES, UNTRUSTED_IMAGE_RULE
from app.model.service import ModelService, model_family
from app.model.types import ModelResult


def make_png(width=8, height=8, pad=0):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(min(height, 8))))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw)
    out += chunk(b"tEXt", b"x" * pad) if pad else b""
    return out + chunk(b"IEND", b"")


GOOD = '{"is_error":false,"result":"{\\"shape\\":\\"triangle\\"}"}'
SCHEMA = {"type": "object"}
MSG = [{"role": "user", "content": "Что на рисунке?"}]


def make_post(calls):
    return lambda *a, **k: calls.append(1) or {}


def run(channel, images, seen):
    def spawn(argv, *, input, env, cwd, timeout):
        seen.append(SimpleNamespace(argv=argv, input=input, cwd=cwd))
        return SimpleNamespace(returncode=0, stdout=GOOD, stderr="")

    payload = {
        "channel": channel,
        "model": "m",
        "system": "sys",
        "messages": MSG,
        "schema": SCHEMA,
        "images": [base64.b64encode(i).decode() for i in images],
    }
    return handle_request(payload, env={}, spawn=spawn)


def test_png_attachment_reaches_both_adapters_within_limits(tmp_path):
    png = make_png()
    seen = []
    reply = run("claude", [png], seen)
    assert reply["error"] is None and reply["json"] == {"shape": "triangle"}
    call = seen[0]
    assert call.argv.count("stream-json") == 2 and "--verbose" in call.argv
    line = json.loads(call.input)
    block = line["message"]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == png
    assert "недоверенные данные" in " ".join(call.argv)

    # cursor: PNG лежит файлом в каталоге запуска, путь - в запросе.
    files = {}

    def spawn(argv, *, input, env, cwd, timeout):
        import os

        files.update({n: Path(cwd, n).read_bytes() for n in os.listdir(cwd)})
        files["_input"] = input
        return SimpleNamespace(returncode=0, stdout=GOOD, stderr="")

    payload = {
        "channel": "cursor", "model": "m", "system": "sys", "messages": MSG, "schema": None,
        "images": [base64.b64encode(png).decode()],
    }  # fmt: skip
    reply = handle_request(payload, env={}, spawn=spawn, workroot=str(tmp_path))
    assert reply["error"] is None
    assert files["image-1.png"] == png
    assert "./image-1.png" in files["_input"] and "недоверенные данные" in files["_input"]
    assert UNTRUSTED_IMAGE_RULE.strip().startswith("К запросу")

    # без изображений запрос прежний
    seen.clear()
    plain = {"channel": "claude", "model": "m", "system": "s", "messages": MSG}
    handle_request(plain, env={}, spawn=lambda *a, **k: seen.append(k) or SimpleNamespace(
        returncode=0, stdout=GOOD, stderr=""))  # fmt: skip
    assert "stream-json" not in seen[0]["input"]

    # сверх предела: понятная ошибка до вызова CLI, на стороне клиента и исполнителя
    cases = {
        "image_too_large": [make_png(pad=MAX_IMAGE_BYTES)],
        "image_too_many_pixels": [make_png(width=3000, height=3000)],
        "too_many_images": [png] * (MAX_IMAGES + 1),
        "bad_image": [b"not a png at all, just text bytes"],
    }
    for kind, images in cases.items():
        calls = []
        post = make_post(calls)
        res = ExecutorChannel("claude", "http://x", key="k", post=post).complete(
            "s", MSG, images=images
        )
        assert res.error_kind == kind and not calls, kind
        seen.clear()
        assert run("claude", images, seen)["error_kind"] == kind and not seen, kind

    # полный размер запроса: четыре крупных PNG укладываются в предел по отдельности, но не вместе
    big = [make_png(pad=MAX_IMAGE_BYTES - 200)] * MAX_IMAGES
    calls = []
    res = ExecutorChannel(
        "claude", "http://x", key="k", post=lambda *a, **k: calls.append(1)
    ).complete("s", MSG, images=big)
    assert res.error_kind == "request_too_large" and not calls

    # в пределах клиент кладёт base64 в тело запроса
    sent = {}
    ok = ExecutorChannel(
        "claude",
        "http://x",
        key="k",
        post=lambda u, p, t, headers=None: sent.update(p) or {"text": "t"},
    ).complete("s", MSG, images=[png])
    assert ok.ok and base64.b64decode(sent["images"][0]) == png


class Down:
    def __init__(self, name):
        self.name, self.calls = name, 0

    def complete(self, system, messages, schema=None, *, model=None, images=None):
        self.calls += 1
        return ModelResult.failure(self.name, model or "", "timeout")


def test_family_is_pinned_and_no_fallback(tmp_path):
    conn = connect(tmp_path / "o.db")
    migrate(conn)
    down, spare = Down("claude"), Down("cursor")
    svc = ModelService(conn, {"claude": down, "cursor": spare})
    # по умолчанию модели явные и семейства разные
    assert svc.family("claude") == "anthropic"
    assert svc.family("cursor") == "openai"
    conn.execute(
        "UPDATE settings SET claude_model='claude-sonnet-4-5', cursor_model='gemini-2.5-pro'"
    )
    assert (svc.family("claude"), svc.family("cursor")) == ("anthropic", "google")
    for value in ("auto", "mystery-model"):
        conn.execute("UPDATE settings SET cursor_model=?", (value,))
        assert svc.family("cursor") is None
    assert svc.family("nope") is None
    assert model_family("gpt-5.1") == "openai" and model_family("opus") == "anthropic"
    assert model_family("goose") is None and model_family(None) is None

    res = svc.complete_on("claude", "s", MSG, images=[make_png()])
    assert not res.ok and res.channel == "claude"
    assert down.calls == 1 and spare.calls == 0
    assert svc.complete_on("zzz", "s", MSG).error_kind == "bad_channel"


def test_stream_output_truncated_png_oversize_text_and_facade(tmp_path):
    from app.ingest.context import ExecutorModel
    from app.model.adapters import ClaudeAdapter
    from app.model.client import MAX_REQUEST_BYTES

    init = '{"type":"system","subtype":"init"}'
    final = '{"type":"result","is_error":false,"result":"{\\"a\\":1}","usage":{"input_tokens":1,"output_tokens":2}}'
    stream = f"{init}\n{final}"
    parsed = ClaudeAdapter().parse(stream, {"type": "object"})
    assert parsed.json == {"a": 1} and parsed.usage["output_tokens"] == 2

    png = make_png()
    for bad in (png[:24], png[:-5], png[:-12], png[:40] + b"\x00" + png[41:]):
        seen = []
        assert run("claude", [bad], seen)["error_kind"] == "bad_image" and not seen

    calls = []
    huge = [{"role": "user", "content": "я" * MAX_REQUEST_BYTES}]
    res = ExecutorChannel("claude", "http://x", key="k", post=make_post(calls)).complete("s", huge)
    assert res.error_kind == "request_too_large" and not calls

    db = tmp_path / "o.db"
    migrate(connect(db))
    model = ExecutorModel(db)
    assert model.family("claude") == "anthropic"
    res = model.complete_on("claude", "s", MSG, None, None, [png, png, png, png, png])
    assert res.error_kind == "too_many_images"
