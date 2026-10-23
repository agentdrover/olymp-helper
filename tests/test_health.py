from fastapi.testclient import TestClient

from app.db import SCHEMA_VERSION
from app.main import create_app


def test_ac1(tmp_path):
    db = tmp_path / "olymp.db"
    with TestClient(create_app(db_path=db)) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == SCHEMA_VERSION
