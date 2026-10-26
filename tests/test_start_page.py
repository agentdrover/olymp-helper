from fastapi.testclient import TestClient

from app.main import create_app


def test_the_root_offers_the_child_and_the_parent_entrances(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "olymp.db")) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'href="/tasks"' in resp.text
    assert 'href="/parent/login"' in resp.text
    # Стартовая страница не читает банк: ни заданий, ни данных ребёнка на ней нет.
    assert "/tasks/" not in resp.text
