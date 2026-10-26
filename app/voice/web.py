"""Маршрут голоса (ребёнок): запись -> текст для поля ввода. Текст НЕ уходит модели сам: ребёнок
правит его и отправляет сам. Те же замки, что у остальных POST ребёнка: свой источник, JSON,
уведомление прочитано. Аудио приходит в JSON (base64): так остаётся защита по типу содержимого."""

import base64
import binascii
import json

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.ingest.store import atomic
from app.ingest.web import child_notice_gate
from app.tutor import state
from app.tutor.web import Db, _published, child_same_origin
from app.voice.service import MAX_BYTES, VoiceError

router = APIRouter()
BODY_LIMIT = MAX_BYTES * 4 // 3 + 4096  # base64 и обёртка JSON


def _failure(exc: VoiceError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": exc.code, "message": exc.message}, status_code=exc.status
    )


async def _body(request: Request) -> bytes:
    """Тело запроса не больше лимита: лишнее не читается."""
    too_big = VoiceError("too_big", 413, "Запись больше 2 МБ. Запиши короче.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_LIMIT:
        raise too_big
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > BODY_LIMIT:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)


def _audio(raw: bytes) -> tuple[bytes, str]:
    bad = VoiceError("bad_request", 422, "Не получилось отправить запись. Запиши ещё раз.")
    try:
        body = json.loads(raw)
        data = base64.b64decode(body["audio"], validate=True)
        mime = str(body.get("mime") or "")
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise bad from None
    return data, mime


@router.post(
    "/tasks/{task_id}/voice",
    dependencies=[Depends(child_same_origin), Depends(child_notice_gate)],
)
async def voice(task_id: int, request: Request, conn: Db):
    _published(conn, task_id)
    try:
        data, mime = _audio(await _body(request))
        text, seconds = await run_in_threadpool(request.app.state.voice.recognize, data, mime)
    except VoiceError as exc:
        return _failure(exc)
    with atomic(conn):
        state.add_event(
            conn, request.app.state.clock(), "voice", task_id,
            {"seconds": round(seconds, 1), "text": text},
        )  # fmt: skip
    return JSONResponse({"ok": True, "text": text})
