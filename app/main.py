import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.db import connect, migrate, schema_version

DEFAULT_DB_PATH = "/srv/olymp/olymp.db"


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path or os.environ.get("OLYMP_DB", DEFAULT_DB_PATH))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(path)
        migrate(conn)
        conn.close()
        app.state.db_path = path
        yield

    app = FastAPI(title="olymp-helper", lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        conn = connect(app.state.db_path)
        try:
            return {"status": "ok", "schema_version": schema_version(conn)}
        finally:
            conn.close()

    return app
