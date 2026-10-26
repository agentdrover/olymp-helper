"""Voice input (OH-T07): ffmpeg conversion, deletion of the audio, limits, queue of one."""

import base64
import shutil
import subprocess
import threading
import wave

from fastapi.testclient import TestClient

from app.voice.service import MAX_BYTES, VoiceService
from tests.bank_helpers import CHILD, build_app
from tests.chat_helpers import FIXTURE_WEBM, Recognizer, boot_chat, voice_post
from tests.tutor_helpers import Scripted, events, seed


def test_ac4(tmp_path):
    assert shutil.which("ffmpeg"), "ffmpeg is required: the conversion is tested for real"
    seen = {}

    def recognize(wav, timeout):
        with wave.open(str(wav)) as w:
            seen.update(
                channels=w.getnchannels(),
                rate=w.getframerate(),
                width=w.getsampwidth(),
                seconds=w.getnframes() / w.getframerate(),
                exists=wav.exists(),
            )
        return "привет клетка"

    rec = Recognizer(fn=recognize)
    c, _model, _voice, audio = boot_chat(tmp_path, recognizer=rec)
    with c:
        tid = seed(tmp_path, key="Б")
        # the Chrome-style WebM/Opus recording (no duration in the header) is decoded by ffmpeg
        # itself: mono, 16 kHz, signed 16-bit little endian, about two seconds
        r = voice_post(c, tid)
        assert r.status_code == 200 and r.json() == {"ok": True, "text": "привет клетка"}
        assert seen["channels"] == 1 and seen["rate"] == 16000 and seen["width"] == 2
        assert 1.5 < seen["seconds"] < 2.6 and seen["exists"]
        # the audio is gone as soon as the text is known: nothing is left in the work area
        assert not rec.paths[0].exists() and list(audio.iterdir()) == []
        # the voice event keeps the text for the parent, never the audio
        ev = events(tmp_path, "voice")
        assert len(ev) == 1 and ev[0]["data"]["text"] == "привет клетка"
        assert "audio" not in ev[0]["data"]

        # a failing recognizer deletes the audio too
        def boom(wav, timeout):
            raise RuntimeError("whisper died")

        rec.fn = boom
        r = voice_post(c, tid)
        assert r.status_code == 503 and r.json()["ok"] is False
        assert list(audio.iterdir()) == []
        # not an audio file at all: a clear refusal, nothing left
        rec.fn = None
        r = voice_post(c, tid, data=b"this is not audio" * 100)
        assert r.status_code == 422 and r.json()["code"] == "bad_audio"
        assert list(audio.iterdir()) == []


def _tone(seconds: int, path) -> bytes:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=330:duration={seconds}", "-ar", "48000", "-ac", "1",
         "-c:a", "libopus", "-b:a", "16k", "-f", "webm", str(path)],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


def test_ac5(tmp_path):
    # the recognizer is stubbed; the limits are the server's
    rec = Recognizer("слово")
    c, _model, _voice, audio = boot_chat(tmp_path / "a", recognizer=rec)
    with c:
        tid = seed(tmp_path / "a", key="Б")
        # more than 2 MB is refused before anything is converted or recognized
        big = voice_post(c, tid, data=b"\0" * (MAX_BYTES + 1))
        assert big.status_code == 413 and big.json()["code"] == "too_big"
        assert rec.paths == [] and list(audio.iterdir()) == []
        # longer than 30 s: a clear message, no recognition
        long = voice_post(c, tid, data=_tone(40, tmp_path / "long.webm"))
        assert long.status_code == 422 and long.json()["code"] == "too_long"
        assert "30 с" in long.json()["message"] and rec.paths == []
        assert list(audio.iterdir()) == []
        # a recording within the limits goes through
        assert voice_post(c, tid).status_code == 200 and len(rec.paths) == 1

    # recognition that takes longer than the timeout (60 s in production): a clear error
    seen = {}

    def slow(wav, timeout):
        seen["timeout"] = timeout
        raise subprocess_timeout()

    rec = Recognizer(fn=slow)
    c, _model, voice, audio = boot_chat(tmp_path / "b", recognizer=rec)
    with c:
        tid = seed(tmp_path / "b", key="Б")
        r = voice_post(c, tid)
        assert r.status_code == 504 and r.json()["code"] == "timeout"
        assert "60 с" in r.json()["message"] and 0 < seen["timeout"] <= 60
        assert list(audio.iterdir()) == []
        assert voice.timeout_s == 60

    # a recognizer that really runs too long is killed by the timeout
    script = tmp_path / "slow-whisper.sh"
    script.write_text("#!/bin/sh\nsleep 20\n")
    script.chmod(0o755)
    fast = VoiceService.whisper(
        str(script), "/nonexistent-model", workdir=tmp_path / "w", timeout_s=2
    )
    app = build_app(tmp_path / "c", Scripted(), voice=fast)
    with TestClient(app) as c:
        tid = seed(tmp_path / "c", key="Б")
        r = voice_post(c, tid)
        assert r.status_code == 504 and r.json()["code"] == "timeout"
        assert not any((tmp_path / "w").iterdir())

    # queue of one: while one recording is being recognized the next one is told to wait
    gate = threading.Event()
    entered = threading.Event()

    def hold(wav, timeout):
        entered.set()
        gate.wait(20)
        return "первая запись"

    rec = Recognizer(fn=hold)
    c, _model, _voice, audio = boot_chat(tmp_path / "d", recognizer=rec)
    with c:
        tid = seed(tmp_path / "d", key="Б")
        first = {}
        t = threading.Thread(target=lambda: first.update(r=voice_post(c, tid)), daemon=True)
        t.start()
        assert entered.wait(10)
        second = voice_post(c, tid)
        assert second.status_code == 409 and second.json()["code"] == "busy"
        assert "подожди" in second.json()["message"].lower()
        assert len(rec.paths) == 1  # the second recording never reached the recognizer
        gate.set()
        t.join(10)
        assert first["r"].json()["text"] == "первая запись"
        # the slot is free again
        rec.fn = None
        assert voice_post(c, tid).status_code == 200
        assert list(audio.iterdir()) == []


def subprocess_timeout():
    from app.voice.service import RecognizerTimeout

    return RecognizerTimeout()


def test_voice_goes_through_the_same_gates_as_the_other_child_posts(tmp_path):
    c, _model, _voice, _audio = boot_chat(tmp_path, recognizer=Recognizer())
    with c:
        tid = seed(tmp_path, key="Б")
        body = {"audio": base64.b64encode(FIXTURE_WEBM.read_bytes()).decode(), "mime": "audio/webm"}
        url = f"/tasks/{tid}/voice"
        evil = c.post(url, json=body, headers={**CHILD, "Origin": "https://evil.example"})
        assert evil.status_code == 403
        form = c.post(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert form.status_code == 415
        assert c.post(url, json={**body, "mime": "text/html"}, headers=CHILD).status_code == 415
        assert events(tmp_path, "voice") == []
    c2, _m, _v, _a = boot_chat(tmp_path / "n", recognizer=Recognizer(), notice=False)
    with c2:
        tid = seed(tmp_path / "n", key="Б")
        r = voice_post(c2, tid)
        assert r.status_code == 403 and r.json()["detail"]["error"] == "notice_required"
