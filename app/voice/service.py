"""Голос: запись из браузера -> ffmpeg (mono PCM S16LE 16 кГц) -> whisper.cpp (SPEC-1.0 1.6, 1.7).

Всё локально: во время работы ничего не скачивается и наружу не уходит. Запись до 30 с и 2 МБ,
распознавание одно за раз (очередь на 1 задачу), весь путь укладывается в 60 с. Аудио живёт только
в каталоге одного запроса и удаляется сразу после распознавания - при любом исходе.
"""

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 30
SECONDS_SLACK = 0.5  # браузер останавливает запись по таймеру: допуск на округление
TIMEOUT_S = 60
RATE = 16000

# Состояния голоса на экране (SPEC-1.0 1.3а). Показывает страница; тексты здесь одни.
VOICE_STATES = {
    "no_mic": "Нет доступа к микрофону. Разреши его в настройках браузера или напиши вопрос.",
    "insecure": "Страница открыта не по HTTPS, поэтому микрофон не работает. Напиши вопрос.",
    "recognizing": "Распознаю…",
    "too_long": f"Запись длиннее {MAX_SECONDS} с. Запиши ещё раз, покороче.",
}

# Тип записи определяет браузер (MediaRecorder.isTypeSupported). Принимаем WebM (Chrome, Firefox),
# Ogg и MP4/M4A (Safari, audio/mp4). Тип из запроса ничего не решает: формат определяют первые
# байты, и именно этот демультиплексор ffmpeg получает принудительно (-f).
ACCEPTED = {"audio/webm", "video/webm", "audio/ogg", "audio/mp4", "video/mp4"}
DEMUXERS = {
    "webm": "matroska,webm",
    "ogg": "ogg",
    "mp4": "mov,mp4,m4a,3gp,3g2,mj2",
}


def sniff(data: bytes) -> str | None:
    """webm | ogg | mp4 по магическим байтам; всё остальное (манифесты, списки, текст) - None."""
    if data[:4] == b"\x1a\x45\xdf\xa3":  # EBML: WebM / Matroska
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":  # MP4 / M4A
        return "mp4"
    return None


UNAVAILABLE = "Распознавание сейчас недоступно. Напиши вопрос."
log = logging.getLogger("app.voice")
_warned: set[str] = set()


def _warn_once(text: str) -> None:
    """Нет ffmpeg или распознавателя - в журнал один раз, не на каждую запись."""
    if text not in _warned:
        _warned.add(text)
        log.warning(text)


class VoiceError(Exception):
    """Отказ с кодом и текстом, который читает ребёнок."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


class RecognizerTimeout(Exception):
    """Распознавание не уложилось во время."""


def _run(cmd: list[str], timeout: float, data: bytes | None = None) -> tuple[int, str]:
    """Запуск в своей группе процессов: по таймауту убивается вся группа, а не только оболочка."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
    )  # fmt: skip
    try:
        out, _ = proc.communicate(data, timeout=max(timeout, 0.1))
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise RecognizerTimeout from None
    return proc.returncode, (out or b"").decode("utf-8", "replace")


class VoiceService:
    """transcribe(wav: Path, timeout: float) -> str - распознаватель (в работе whisper.cpp)."""

    def __init__(
        self,
        transcribe: Callable[[Path, float], str],
        *,
        workdir: str | Path | None = None,
        timeout_s: float = TIMEOUT_S,
        ffmpeg: str = "ffmpeg",
        runner: Callable[..., tuple[int, str]] | None = None,
    ):
        self.transcribe = transcribe
        self.workdir = None if workdir is None else Path(workdir)
        self.timeout_s = timeout_s
        self.ffmpeg = ffmpeg
        self._runner = runner or _run  # тесты подставляют поддельный запуск ffmpeg
        self._slot = threading.Lock()  # очередь на 1 задачу

    @classmethod
    def whisper(cls, binary: str, model: str, **kw) -> "VoiceService":
        def transcribe(wav: Path, timeout: float) -> str:
            try:
                code, out = _run(
                    [binary, "-m", model, "-f", str(wav), "-l", "ru", "-nt", "-np"], timeout
                )
            except FileNotFoundError as exc:
                _warn_once("whisper is missing: voice input is unavailable")
                raise RuntimeError("recognizer is missing") from exc
            if code != 0:
                raise RuntimeError("recognizer failed")
            return out

        return cls(transcribe, **kw)

    @classmethod
    def from_env(cls, **kw) -> "VoiceService":
        return cls.whisper(
            os.environ.get("WHISPER_BIN", "whisper-cli"),
            os.environ.get("WHISPER_MODEL", "/opt/whisper/ggml-small.bin"),
            **kw,
        )

    # ------------------------------------------------------------------- распознавание

    def recognize(self, data: bytes, mime: str) -> tuple[str, float]:
        """(текст, секунды записи). Бросает VoiceError; аудио удаляется при любом исходе."""
        kind = (mime or "").split(";")[0].strip().lower()
        if kind not in ACCEPTED:
            raise VoiceError("unsupported", 415, "Такой формат записи не поддерживается.")
        if len(data) > MAX_BYTES:
            raise VoiceError("too_big", 413, "Запись больше 2 МБ. Запиши короче.")
        fmt = sniff(data)
        if fmt is None:  # манифест, список воспроизведения, текст: до ffmpeg не доходит
            raise VoiceError("bad_audio", 422, "Не получилось разобрать запись. Запиши ещё раз.")
        if not self._slot.acquire(blocking=False):
            raise VoiceError("busy", 409, "Подожди: я ещё распознаю прошлую запись.")
        try:
            return self._pipeline(data, fmt)
        finally:
            self._slot.release()

    def _pipeline(self, data: bytes, fmt: str) -> tuple[str, float]:
        if self.workdir is not None:
            self.workdir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="voice-", dir=self.workdir))
        deadline = time.monotonic() + self.timeout_s
        try:
            wav = work / "out.wav"
            seconds = self._convert(data, fmt, wav, deadline)
            text = self._transcribe(wav, deadline)
            return text, seconds
        except RecognizerTimeout:
            raise VoiceError(
                "timeout", 504, f"Не успел распознать за {int(self.timeout_s)} с. Запиши короче."
            ) from None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _convert(self, data: bytes, fmt: str, wav: Path, deadline: float) -> float:
        """Вход идёт в ffmpeg только через stdin с принудительным демультиплексором и белым
        списком протоколов `pipe`: содержимое записи не может открыть ни файл, ни сеть.
        Длина ограничена `-t`: дольше MAX_SECONDS + доли секунды не конвертируется."""
        cmd = [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "pipe", "-f", DEMUXERS[fmt], "-i", "pipe:0",
            "-t", str(MAX_SECONDS + SECONDS_SLACK + 0.1), "-vn", "-ac", "1", "-ar", str(RATE),
            "-c:a", "pcm_s16le", "-f", "wav", str(wav),
        ]  # fmt: skip
        try:
            code, _ = self._runner(cmd, deadline - time.monotonic(), data)
        except FileNotFoundError:
            _warn_once("ffmpeg is missing: voice input is unavailable")
            raise VoiceError("unavailable", 503, UNAVAILABLE) from None
        if code != 0 or not wav.exists():
            raise VoiceError("bad_audio", 422, "Не получилось разобрать запись. Запиши ещё раз.")
        try:
            with wave.open(str(wav)) as w:
                seconds = w.getnframes() / w.getframerate()
        except (wave.Error, EOFError, ZeroDivisionError):
            raise VoiceError(
                "bad_audio", 422, "Не получилось разобрать запись. Запиши ещё раз."
            ) from None
        if seconds < 0.1:
            raise VoiceError("bad_audio", 422, "В записи ничего нет. Запиши ещё раз.")
        if seconds > MAX_SECONDS + SECONDS_SLACK:
            raise VoiceError(
                "too_long", 422, f"Запись длиннее {MAX_SECONDS} с. Запиши ещё раз, покороче."
            )
        return seconds

    def _transcribe(self, wav: Path, deadline: float) -> str:
        left = deadline - time.monotonic()
        if left <= 0:
            raise RecognizerTimeout
        try:
            raw = self.transcribe(wav, left)
        except RecognizerTimeout:
            raise
        except Exception:  # noqa: BLE001 - любой сбой распознавателя: ребёнку одно сообщение
            raise VoiceError("unavailable", 503, UNAVAILABLE) from None
        text = " ".join((raw or "").split())
        if not text:
            raise VoiceError(
                "unrecognized", 422, "Ничего не расслышал. Скажи ещё раз или напиши вопрос."
            )
        return text
