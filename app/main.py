import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import auth, privacy
from app.account_web import parent as account_parent
from app.account_web import public as account_public
from app.db import connect, migrate, schema_version
from app.exam_web import child as exam_child
from app.exam_web import parent as exam_parent
from app.ingest import page_normalizer
from app.ingest.context import AppContext, ExecutorModel
from app.ingest.jobs import ImportJobs
from app.ingest.limits import IMPORT_MAX_BASE_S, IMPORT_MAX_PER_CHUNK_S, IMPORT_STALL_S
from app.ingest.model_answer import run_saved_answer
from app.ingest.packs import fail_stale, run_pack
from app.ingest.progress import ImportProgress
from app.ingest.render import PageCache
from app.ingest.store import open_db
from app.ingest.web import parent as parent_router
from app.ingest.web import public as public_router
from app.model.work import ModelWork
from app.sessions import utcnow
from app.summary_web import child as summary_child
from app.summary_web import parent as summary_parent
from app.tutor.panel import Inflight
from app.tutor.steps import StepRunner
from app.tutor.web import ExamBusy
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
    auto_publish: bool = True,
    import_stall: float = IMPORT_STALL_S,
    import_clock=None,
    import_poll: float | None = None,
    import_max_base: float = IMPORT_MAX_BASE_S,
    import_max_per_chunk: float = IMPORT_MAX_PER_CHUNK_S,
    clock=None,
    voice: VoiceService | None = None,
    sync_steps: bool = False,
) -> FastAPI:
    """model - объект с complete(system, messages, schema); тесты подменяют только его.

    clock() - текущее время для сессий родителя, блокировки входа и срока хранения (тесты его
    подменяют).

    voice - распознавание речи (в работе ffmpeg и whisper.cpp из образа); тесты подменяют
    распознаватель.

    sync_imports=True выполняет импорт в потоке запроса (тесты); в работе импорт идёт фоном.

    sync_steps=True выполняет шаг с моделью (подсказка, разбор, чат) в потоке запроса (тесты); в
    работе он идёт в фоне, а экран опрашивает сервер.
    """
    clock = clock or utcnow
    path = Path(db_path or os.environ.get("OLYMP_DB", DEFAULT_DB_PATH))
    data = Path(data_dir or os.environ.get("OLYMP_DATA_DIR") or path.parent)

    def prepare():
        with contextlib.closing(connect(path)) as conn:
            migrate(conn)
            # A job already dispatched may have spent quota. Preserve only the
            # durable queued operations for automatic recovery.
            conn.execute(
                "UPDATE material SET import_status = 'failed', import_error = ?, "
                "run_id = lower(hex(randomblob(16))) "
                "WHERE import_status IN ('queued', 'running') AND NOT EXISTS ("
                "SELECT 1 FROM model_operation WHERE kind = 'import' AND status <> 'done' "
                "AND object_key = material.id || ':' || material.run_id)",
                (INTERRUPTED,),
            )
            fail_stale(conn, INTERRUPTED)
        sweep(path, clock)
        ctx.page_cache.purge_legacy()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db_path = path
        if app.state.model_work is None:
            prepare()
        else:
            app.state.model_work.start(prepare)
        sweep_task = asyncio.create_task(_sweep_forever(path, clock))
        try:
            yield
        finally:
            if app.state.model_work is not None:
                app.state.model_work.close()
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task

    # Описание API наружу не отдаётся: ни ребёнку, ни чужому в сети.
    app = FastAPI(
        title="olymp-helper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.clock = clock
    app.add_exception_handler(ExamBusy, lambda request, exc: exc.response)
    app.state.model_work = ModelWork(path) if model is None else None
    app.state.model = model or ExecutorModel(path, managed=True)
    # Часы одни на всё приложение (clock выше): тест подменяет их, и 60 секунд до подсказки,
    # простой занятия и сессия родителя идут по одному времени.
    app.state.inflight = Inflight()
    app.state.steps = StepRunner(app.state, sync=sync_steps)
    app.state.voice = voice or VoiceService.from_env()
    ctx = AppContext(
        path,
        data,
        app.state,
        PageCache(data / "cache" / "pages"),
        progress=ImportProgress(import_clock or time.monotonic),
        auto_publish=auto_publish,
    )
    ctx.autorun.sync = sync_imports
    app.state.ctx = ctx
    app.state.page_cache = ctx.page_cache
    app.state.imports = ImportJobs(
        ctx,
        sync=sync_imports,
        stall=import_stall,
        clock=import_clock or time.monotonic,
        poll=import_poll,
        max_base=import_max_base,
        max_per_chunk=import_max_per_chunk,
    )
    if app.state.model_work is not None:

        def run_saved_pack(key):
            with contextlib.closing(open_db(path)) as conn:
                run_pack(ctx, conn, int(key))

        def run_answer(key):
            with contextlib.closing(open_db(path)) as conn:
                run_saved_answer(ctx, conn, key)

        def run_saved_page(key):
            with contextlib.closing(open_db(path)) as conn:
                page_normalizer.run_saved_page(ctx, conn, key)

        app.state.model_work.handlers["page_answer"] = run_saved_page
        app.state.model_work.handlers["answer"] = run_answer
        app.state.model_work.handlers["autopublish"] = lambda key: ctx.autorun.run_queued(ctx, key)
        app.state.model_work.handlers["import"] = app.state.imports.run_queued
        app.state.model_work.handlers["pack"] = run_saved_pack
        app.state.model_work.handlers["tutor"] = app.state.steps.run_queued
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(account_parent)
    app.include_router(account_public)
    app.include_router(parent_router)
    app.include_router(tutor_router)
    app.include_router(exam_child)
    app.include_router(exam_parent)
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
