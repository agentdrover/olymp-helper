"""Общая оболочка экранов (OH-UI1): один стиль и своя навигация у каждой роли."""

import re

from fastapi.testclient import TestClient

from tests.bank_helpers import (
    CHILD,
    PARENT,
    StubModel,
    answer_by_number,
    build_app,
    make_task,
    reply,
    task_ids,
    text_pdf,
    upload,
)

PARENT_ONLY = ("/parent/review", "/parent/materials", "/parent/logout")


def started(tmp_path):
    model = StubModel(
        split=reply(make_task(1, "Вопрос номер 1")), answer=answer_by_number({1: "А"})
    )
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    upload(c, text_pdf([["1. вопрос"]]))
    tid = task_ids(tmp_path)[0]
    r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "А"})
    assert r.status_code == 200, r.text
    return c, tid


def test_every_screen_uses_the_shared_style_and_its_role_navigation(tmp_path):
    c, tid = started(tmp_path)
    with c:
        child_pages = ["/", "/tasks", f"/tasks/{tid}"]
        for url in child_pages:
            r = c.get(url, headers=CHILD)
            assert r.status_code == 200, url
            assert 'href="/static/app.css"' in r.text, url
            assert "<style" not in r.text, url
            assert "fonts.googleapis" not in r.text, url
            if url == "/":
                continue
            for link in PARENT_ONLY:
                assert link not in r.text, (url, link)
            assert 'href="/parent/login"' in r.text, url
            assert 'aria-current="page"' in r.text, url

        login = c.get("/parent/login", headers=CHILD)
        assert login.status_code == 200
        assert 'href="/static/app.css"' in login.text
        assert "/parent/logout" not in login.text

        parent_pages = ["/parent/review", f"/parent/review/{tid}", "/parent/materials"]
        for url in parent_pages:
            r = c.get(url, headers=PARENT)
            assert r.status_code == 200, url
            assert 'href="/static/app.css"' in r.text, url
            assert 'action="/parent/logout"' in r.text, url
            assert "Выйти" in r.text, url
            assert re.search(r'aria-current="page"', r.text), url
            assert 'href="/parent/login"' not in r.text, url
        # /parent/tasks/{id} - JSON-описание задания для ленты, у него нет оболочки.
        assert c.get(f"/parent/tasks/{tid}", headers=PARENT).status_code == 200


def test_the_child_shell_never_offers_parent_screens(tmp_path):
    c, tid = started(tmp_path)
    with c:
        for url in ("/tasks", f"/tasks/{tid}"):
            html = c.get(url, headers=CHILD).text
            assert "Выйти" not in html
            assert re.findall(r'href="(/parent/[^"]*)"', html) == ["/parent/login"], url


def test_parent_logout_ends_the_parent_session(tmp_path):
    with TestClient(build_app(tmp_path, StubModel())) as c:
        c.post("/parent/login", data={"key": "test-parent-key"}, follow_redirects=False)
        html = {"Accept": "text/html"}
        assert c.get("/parent/review", headers=html).status_code == 200
        out = c.post("/parent/logout", follow_redirects=False)
        assert out.status_code == 303
        assert out.headers["location"] == "/"
        back = c.get("/parent/review", headers=html, follow_redirects=False)
        assert back.status_code == 303
        assert back.headers["location"] == "/parent/login"
        # запрос скрипта без входа по-прежнему получает 403
        assert c.get("/parent/review").status_code == 403
