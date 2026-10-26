"""OH-T08: parent PIN login, server-side sessions, CSRF, lockout. AC1 and AC2 of the card."""

import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import hash_pin, verify_pin
from tests.bank_helpers import PARENT, TEST_PARENT_KEY, StubModel, build_app

PIN = "2580"

# Routes a child (or nobody) may call. Anything else is a parent route and must refuse an
# unauthenticated caller: a new route that is not listed here and is not protected fails AC1.
PUBLIC = {
    ("GET", "/"),
    ("GET", "/healthz"),
    ("GET", "/parent/login"),
    ("POST", "/parent/login"),
    ("POST", "/parent/setup"),
    ("GET", "/notice"),
    ("POST", "/notice/ack"),
    ("GET", "/tasks"),
    ("GET", "/tasks/{task_id}"),
    ("GET", "/tasks/{task_id}/figure.png"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/search"),
    # разбор задания (OH-T06): все за уведомлением о внешней модели, см. test_tutor_notice.py
    ("GET", "/today"),
    ("GET", "/tasks/{task_id}/tutor"),
    ("GET", "/api/tasks/{task_id}/tutor"),
    ("POST", "/tasks/{task_id}/attempt"),
    ("POST", "/tasks/{task_id}/hint"),
    ("POST", "/tasks/{task_id}/explain"),
    ("POST", "/tasks/{task_id}/show-answer"),
    ("POST", "/tasks/{task_id}/repeat"),
    ("POST", "/tasks/{task_id}/dispute"),
}
FRAMEWORK = {"/static"}  # the API description is switched off: no /docs, /redoc, /openapi.json


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def fresh(tmp_path, clock=None):
    """An app with no PIN and no seeded session: the state of a first run."""
    return build_app(tmp_path, StubModel(), parent=False, clock=clock)


def set_pin(client, pin=PIN):
    r = client.post(
        "/parent/setup",
        data={"key": TEST_PARENT_KEY, "pin": pin, "pin2": pin},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


def login(client, pin=PIN):
    return client.post("/parent/login", data={"pin": pin}, follow_redirects=False)


def csrf_of(client):
    html = client.get("/parent/materials").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def flat_routes(routes):
    """FastAPI wraps an included router in one object; walk through it to the real routes."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from flat_routes(inner.routes)
        else:
            yield route


def parent_routes(app):
    out = []
    for route in flat_routes(app.routes):
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                out.append((method, route.path))
    return sorted(out)


def concrete(path):
    return re.sub(r"\{[^}]+\}", "1", path)


# ------------------------------------------------------------------------------ AC1


def test_ac1(tmp_path):
    app = fresh(tmp_path)
    routes = parent_routes(app)
    guarded = [r for r in routes if r not in PUBLIC]
    assert len(guarded) >= 20, guarded  # the walk really sees the parent routes
    assert PUBLIC <= set(routes), PUBLIC - set(routes)  # the allowlist does not rot
    non_api = {
        getattr(r, "path", None) for r in flat_routes(app.routes) if not isinstance(r, APIRoute)
    }
    assert non_api <= FRAMEWORK, non_api - FRAMEWORK
    with TestClient(app, raise_server_exceptions=False) as c:
        for hidden in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(hidden).status_code == 404, hidden
        for method, path in guarded:
            url = concrete(path)
            for headers in ({}, {"Cookie": "olymp_parent=forged"}, {"Accept": "text/html"}):
                r = c.request(
                    method,
                    url,
                    headers=headers,
                    json={} if method == "POST" else None,
                    follow_redirects=False,
                )
                assert r.status_code in (303, 403), (method, path, headers, r.status_code)
                if r.status_code == 303:
                    assert r.headers["location"] == "/parent/login"
        # with a valid session the same walk gets past the guard
        set_pin(c)
        token = csrf_of(c)
        for method, path in guarded:
            if path == "/parent/logout":
                continue
            r = c.request(
                method,
                concrete(path),
                headers={"X-CSRF-Token": token},
                json={} if method == "POST" else None,
                follow_redirects=False,
            )
            assert r.status_code != 403, (method, path)
            assert r.headers.get("location") != "/parent/login", (method, path)


# ------------------------------------------------------------------------------ AC2


def test_ac2(tmp_path):
    clock = Clock()
    app = fresh(tmp_path, clock)

    # lockout is kept on the server: a new cookie jar changes nothing
    with TestClient(app) as first:
        set_pin(first)
        first.post("/parent/logout", headers={"X-CSRF-Token": csrf_of(first)})
        for _ in range(5):
            assert login(first, "0000").status_code in (401, 429)
        assert login(first, PIN).status_code == 429
    with TestClient(app) as other:  # no cookies at all
        r = login(other, PIN)
        assert r.status_code == 429 and "olymp_parent" not in r.headers.get("set-cookie", "")
        assert other.get("/parent/materials", follow_redirects=False).status_code == 403
        clock.advance(minutes=4, seconds=59)
        assert login(other, PIN).status_code == 429
        clock.advance(seconds=2)
        assert login(other, PIN).status_code == 303

    # 30 minutes of idleness end the session on the server
    with TestClient(app) as c:
        assert login(c).status_code == 303
        assert c.get("/parent/materials").status_code == 200
        clock.advance(minutes=29)
        assert c.get("/parent/materials").status_code == 200  # activity renews the session
        clock.advance(minutes=29)
        assert c.get("/parent/materials").status_code == 200
        clock.advance(minutes=31)
        assert c.get("/parent/materials", follow_redirects=False).status_code == 403
        html = c.get("/parent/materials", headers={"Accept": "text/html"}, follow_redirects=False)
        assert html.status_code == 303 and html.headers["location"] == "/parent/login"
        conn = sqlite3.connect(tmp_path / "olymp.db")
        mine = hashlib.sha256(c.cookies["olymp_parent"].encode()).hexdigest()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parent_session WHERE id_hash = ?", (mine,)
            ).fetchone()[0]
            == 0
        )
        conn.close()

    # every state-changing parent route refuses a request without the session's CSRF token
    with TestClient(app) as c:
        assert login(c).status_code == 303
        token = csrf_of(c)
        mutating = [(m, p) for m, p in parent_routes(app) if (m, p) not in PUBLIC and m != "GET"]
        assert len(mutating) >= 10, mutating
        for method, path in mutating:
            url = concrete(path)
            assert c.request(method, url, json={}).status_code == 403, (method, path)
            bad = {"X-CSRF-Token": "nope"}
            assert c.request(method, url, json={}, headers=bad).status_code == 403, path
            assert c.request(method, url, data={"csrf": "nope"}).status_code == 403, path
        # the right token in the header, or in the form field, gets past the check
        ok = c.post("/parent/settings", headers={"X-CSRF-Token": token}, data={})
        assert ok.status_code != 403
        ok = c.post("/parent/settings", data={"csrf": token}, follow_redirects=False)
        assert ok.status_code == 303
        # a token of another session is worth nothing
        with TestClient(app) as second:
            assert login(second).status_code == 303
            other_token = csrf_of(second)
        assert other_token != token
        r = c.post("/parent/settings", headers={"X-CSRF-Token": other_token}, data={})
        assert r.status_code == 403
        # logout needs the token too, and ends the session on the server
        assert c.post("/parent/logout").status_code == 403
        assert (
            c.post("/parent/logout", data={"csrf": token}, follow_redirects=False).status_code
            == 303
        )
        assert c.get("/parent/materials", follow_redirects=False).status_code == 403


# ---------------------------------------------------------------------- bootstrap and PIN


def test_pin_setup_needs_the_key_and_works_once(tmp_path):
    app = fresh(tmp_path)
    with TestClient(app) as c:
        page = c.get("/parent/login").text
        assert "Задайте PIN" in page and 'name="key"' in page
        bad = c.post("/parent/setup", data={"key": "wrong", "pin": PIN, "pin2": PIN})
        assert bad.status_code == 403
        assert login(c, PIN).status_code == 403  # no PIN yet: nothing to log in with
        assert (
            c.post(
                "/parent/setup", data={"key": TEST_PARENT_KEY, "pin": "12", "pin2": "12"}
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/parent/setup", data={"key": TEST_PARENT_KEY, "pin": PIN, "pin2": "1111"}
            ).status_code
            == 422
        )
        set_pin(c)
        conn = sqlite3.connect(tmp_path / "olymp.db")
        stored = conn.execute("SELECT parent_pin_hash FROM settings").fetchone()[0]
        conn.close()
        assert PIN not in stored and stored.startswith("scrypt$")
    with TestClient(app) as late:
        # once a PIN exists the key is no longer a way in, and cannot reset the PIN
        assert "Задайте PIN" not in late.get("/parent/login").text
        r = late.post("/parent/setup", data={"key": TEST_PARENT_KEY, "pin": "1111", "pin2": "1111"})
        assert r.status_code == 403
        assert login(late, "1111").status_code == 401
        assert login(late, PIN).status_code == 303


def test_setup_without_a_configured_key_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMP_PARENT_KEY")
    with TestClient(fresh(tmp_path)) as c:
        r = c.post("/parent/setup", data={"key": "", "pin": PIN, "pin2": PIN})
        assert r.status_code == 403
        assert "не настроен" in c.get("/parent/login").text


def test_the_pin_is_stored_as_a_salted_kdf_hash():
    a, b = hash_pin(PIN), hash_pin(PIN)
    assert a != b and verify_pin(PIN, a) and verify_pin(PIN, b)
    assert not verify_pin("2581", a) and not verify_pin("", a) and not verify_pin(PIN, "junk")


def test_the_session_cookie_is_httponly_and_strict(tmp_path):
    with TestClient(fresh(tmp_path)) as c:
        cookie = set_pin(c).headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        sid = c.cookies["olymp_parent"]
        conn = sqlite3.connect(tmp_path / "olymp.db")
        rows = [r[0] for r in conn.execute("SELECT id_hash FROM parent_session")]
        conn.close()
        assert sid not in rows and len(rows) == 1  # only a hash of the id is stored


def test_seeded_parent_header_still_opens_the_parent_pages(tmp_path):
    with TestClient(build_app(tmp_path, StubModel())) as c:
        assert c.get("/parent/materials", headers=PARENT).status_code == 200


@pytest.mark.parametrize("path", ["/parent/review", "/parent/materials", "/parent/settings"])
def test_html_pages_redirect_a_browser_to_the_pin_screen(tmp_path, path):
    with TestClient(fresh(tmp_path)) as c:
        r = c.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/parent/login"


def test_every_parent_post_form_carries_the_csrf_field(tmp_path):
    """A plain HTML form cannot send the X-CSRF-Token header: without the hidden field the
    browser gets 403 on submit (found when G2's pack form met T08)."""
    with TestClient(build_app(tmp_path, StubModel())) as c:
        for path in ("/parent/materials", "/parent/generate", "/parent/settings"):
            html = c.get(path, headers=PARENT).text
            forms = re.findall(
                r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", html, re.DOTALL | re.IGNORECASE
            )
            assert forms, path
            for body in forms:
                assert 'name="csrf"' in body, path
