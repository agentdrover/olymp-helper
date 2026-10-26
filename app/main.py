import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import connect, migrate, schema_version
from app.ingest.context import AppContext, ExecutorModel
from app.ingest.jobs import ImportJobs
from app.ingest.limits import IMPORT_TIMEOUT_S
from app.ingest.render import PageCache
from app.ingest.web import parent as parent_router
from app.ingest.web import public as public_router
from app.tutor.panel import Inflight
from app.tutor.web import router as tutor_router

DEFAULT_DB_PATH = "/srv/olymp/olymp.db"
INTERRUPTED = "прервано перезапуском"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    db_path: str | Path | None = None,
    *,
    model=None,
    data_dir: str | Path | None = None,
    sync_imports: bool = False,
    import_timeout: float = IMPORT_TIMEOUT_S,
    clock=None,
) -> FastAPI:
    """model - объект с complete(system, messages, schema); тесты подменяют только его.

    sync_imports=True выполняет импорт в потоке запроса (тесты); в работе импорт идёт фоном.
    """
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
        conn.close()
        app.state.db_path = path
        yield

    app = FastAPI(title="olymp-helper", lifespan=lifespan)
    app.state.model = model or ExecutorModel(path)
    # Часы занятия и разбора: тест подменяет их, чтобы 60 секунд проверялись без ожидания.
    app.state.clock = clock or (lambda: datetime.now(UTC))
    app.state.inflight = Inflight()
    ctx = AppContext(path, data, app.state, PageCache(data / "cache" / "pages"))
    app.state.ctx = ctx
    app.state.page_cache = ctx.page_cache
    app.state.imports = ImportJobs(ctx, sync=sync_imports, timeout=import_timeout)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(parent_router)
    app.include_router(tutor_router)
    app.include_router(public_router)

    @app.get("/healthz")
    def healthz():
        conn = connect(app.state.db_path)
        try:
            return {"status": "ok", "schema_version": schema_version(conn)}
        finally:
            conn.close()

    return app
