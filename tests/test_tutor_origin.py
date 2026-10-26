"""Child POSTs refuse foreign pages and plain HTML forms (OH-T06, Codex P2)."""

from tests.bank_helpers import db
from tests.tutor_helpers import CHILD, attempt, boot, seed, view

EVIL = {"Origin": "https://evil.example"}
ACTIONS = ("attempt", "hint", "explain", "show-answer", "repeat", "dispute")


def state_of(tmp_path):
    conn = db(tmp_path)
    return conn.execute(
        "SELECT COALESCE(SUM(hints), 0), COALESCE(SUM(attempts), 0) FROM tutor_run"
    ).fetchone()[:]


def test_cross_origin_form_post_is_refused_without_a_model_call_or_state_change(tmp_path):
    c, _clock, model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "А")
        calls, before = len(model.calls), state_of(tmp_path)
        for action in ACTIONS:
            r = c.post(
                f"/tasks/{tid}/{action}",
                data={},
                headers={**EVIL, "Content-Type": "application/x-www-form-urlencoded"},
            )
            assert r.status_code == 403, action
            assert r.json()["detail"]["error"] == "cross_origin", action
        assert len(model.calls) == calls and state_of(tmp_path) == before


def test_fetch_metadata_and_origin_rules(tmp_path):
    c, _clock, model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "А")
        url = f"/tasks/{tid}/hint"
        for site in ("cross-site", "same-site", "weird"):  # не same-origin и не none
            assert (
                c.post(url, json={"level": 1}, headers={"Sec-Fetch-Site": site}).status_code == 403
            )
        # чужой порт и чужой хост - чужой источник
        for origin in ("http://testserver:9999", "http://evil.example", "null"):
            assert c.post(url, json={"level": 1}, headers={"Origin": origin}).status_code == 403
        assert view(c, tid)["hints"] == 0 and "hint1" not in model.kinds()
        # свой источник: Origin как у запроса, Sec-Fetch-Site same-origin или none
        ok = c.post(
            url,
            json={"level": 1},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert ok.status_code == 200 and ok.json()["hints"] == 1


def test_json_without_origin_headers_is_allowed_and_forms_are_not(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        # старые клиенты и тесты: ни Origin, ни Sec-Fetch-Site, но JSON
        assert attempt(c, tid, "А").status_code == 200
        # форма без Origin: нельзя - обычная HTML-форма JSON послать не может
        r = c.post(f"/tasks/{tid}/hint", data={}, headers=CHILD)
        assert r.status_code == 415 and r.json()["detail"]["error"] == "json_required"
        assert (
            c.post(
                f"/tasks/{tid}/hint", content=b"{}", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        # форма уведомления - не маршрут разбора: у неё свой одноразовый токен и проверка источника
        assert view(c, tid)["hints"] == 0
