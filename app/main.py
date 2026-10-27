import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import auth, privacy
from app.account_web import parent as account_parent
from app.account_web import public as account_public
from app.db import connect, migrate, schema_version
from app.ingest.context import AppContext, ExecutorModel
from app.ingest.jobs import ImportJobs
from app.ingest.limits import IMPORT_TIMEOUT_S
from app.ingest.packs import fail_stale
from app.ingest.render import PageCache
from app.ingest.web import parent as parent_router
from app.ingest.web import public as public_router
from app.sessions import utcnow
from app.summary_web import child as summary_child
from app.summary_web import parent as summary_parent
from app.tutor.panel import Inflight
from app.tutor.web import router as tutor_router
from app.voice.service import VoiceService
from app.voice.web import router as voice_router

DEFAULT_DB_PATH = "/srv/olymp/olymp.db"
INTERRUPTED = "прервано перезапуском"
STATIC_DIR = Path(__file__).parent / "static"


SWEEP_EVERY_S = 6 * 3600


def sweep(path: Path, clock) -> None:
    """Срок хранения переписки (SPEC 1.6) и стёртые простоем сессии родителя."""
    conn = connect(path)
    try:
        now = clock()
        privacy.cleanup_retention(conn, now)
        auth.purge_sessions(conn, now)
    finally:
        conn.close()


async def _sweep_forever(path: Path, clock) -> None:
    while True:
        await asyncio.sleep(SWEEP_EVERY_S)
        await asyncio.to_thread(sweep, path, clock)


def create_app(
    db_path: str | Path | None = None,
    *,
    model=None,
    data_dir: str | Path | None = None,
    sync_imports: bool = False,
    import_timeout: float = IMPORT_TIMEOUT_S,
    clock=None,
    voice: VoiceService | None = None,
) -> FastAPI:
    """model - объект с complete(system, messages, schema); тесты подменяют только его.

    clock() - текущее время для сессий родителя, блокировки входа и срока хранения (тесты его
    подменяют).

    voice - распознавание речи (в работе ffmpeg и whisper.cpp из образа); тесты подменяют
    распознаватель.

    sync_imports=True выполняет импорт в потоке запроса (тесты); в работе импорт идёт фоном.
    """
    clock = clock or utcnow
    path = Path(db_path or os.environ.get("OLYMP_DB", DEFAULT_DB_PATH))
    data = Path(data_dir or os.environ.get("OLYMP_DATA_DIR") or path.parent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(path)
        migrate(conn)
        # Потоки импорта и их таймеры живут только в памяти процесса: после перезапуска
        # queued/running зависли бы навсегда. Повтор возможен для failed.
        conn.execute(
            "UPDATE material SET import_status = 'failed', import_error = ?, "
            "run_id = lower(hex(randomblob(16))) "
            "WHERE import_status IN ('queued', 'running')",
            (INTERRUPTED,),
        )
        fail_stale(conn, INTERRUPTED)  # запрос к модели за пачкой не переживает перезапуск
        conn.close()
        sweep(path, clock)
        ctx.page_cache.purge_legacy()
        app.state.db_path = path
        sweep_task = asyncio.create_task(_sweep_forever(path, clock))
        try:
            yield
        finally:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task

    # Описание API наружу не отдаётся: ни ребёнку, ни чужому в сети.
    app = FastAPI(
        title="olymp-helper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.clock = clock
    app.state.model = model or ExecutorModel(path)
    # Часы одни на всё приложение (clock выше): тест подменяет их, и 60 секунд до подсказки,
    # простой занятия и сессия родителя идут по одному времени.
    app.state.inflight = Inflight()
    app.state.voice = voice or VoiceService.from_env()
    ctx = AppContext(path, data, app.state, PageCache(data / "cache" / "pages"))
    app.state.ctx = ctx
    app.state.page_cache = ctx.page_cache
    app.state.imports = ImportJobs(ctx, sync=sync_imports, timeout=import_timeout)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(account_parent)
    app.include_router(account_public)
    app.include_router(parent_router)
    app.include_router(tutor_router)
    app.include_router(summary_child)
    app.include_router(summary_parent)
    app.include_router(voice_router)
    app.include_router(public_router)

    @app.get("/healthz")
    def healthz():
        conn = connect(app.state.db_path)
        try:
            return {"status": "ok", "schema_version": schema_version(conn)}
        finally:
            conn.close()

    return app
