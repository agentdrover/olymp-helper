"""Уведомление о внешней модели закрывает ВСЕ маршруты разбора ребёнка (OH-T08 + OH-T06)."""

import re

from tests.bank_helpers import db
from tests.tutor_helpers import CHILD, PARENT, attempt, boot, post, seed, view

HTML_ROUTES = ("/tasks/{id}", "/tasks/{id}/tutor", "/today")
JSON_GETS = ("/api/tasks/{id}/tutor",)
POSTS = ("attempt", "hint", "explain", "show-answer", "repeat", "dispute")


def test_html_routes_redirect_to_the_notice_before_it_is_seen(tmp_path):
    c, _clock, _model = boot(tmp_path, notice=False)
    with c:
        tid = seed(tmp_path)
        for template in HTML_ROUTES:
            url = template.format(id=tid)
            r = c.get(url, headers=CHILD, follow_redirects=False)
            assert r.status_code == 303, url
            assert r.headers["location"].startswith("/notice?next="), url
        # родителю уведомление не нужно
        assert c.get(f"/tasks/{tid}", headers=PARENT, follow_redirects=False).status_code == 200


def test_json_and_post_routes_answer_403_notice_required_before_it_is_seen(tmp_path):
    c, _clock, model = boot(tmp_path, notice=False)
    with c:
        tid = seed(tmp_path)
        for template in JSON_GETS:
            r = c.get(template.format(id=tid), headers=CHILD)
            assert r.status_code == 403 and r.json()["detail"]["error"] == "notice_required"
        for action in POSTS:
            r = post(c, tid, action, answer="А", level=1, mode="simpler", confirm=True)
            assert r.status_code == 403, action
            assert r.json()["detail"]["error"] == "notice_required", action
        assert (
            model.calls == []
            and db(tmp_path).execute("SELECT COUNT(*) FROM tutor_run").fetchone()[0] == 0
        )


def test_every_route_works_after_the_notice_is_seen(tmp_path):
    c, _clock, _model = boot(tmp_path, notice=False)
    with c:
        tid = seed(tmp_path)
        page = c.get(f"/tasks/{tid}", headers=CHILD, follow_redirects=False)
        token = re.search(
            r'name="token" value="([^"]+)"', c.get(page.headers["location"]).text
        ).group(1)
        r = c.post(
            "/notice/ack", data={"next": f"/tasks/{tid}", "token": token}, follow_redirects=False
        )
        assert r.status_code == 303
        for template in HTML_ROUTES:
            assert c.get(template.format(id=tid), headers=CHILD).status_code == 200, template
        assert view(c, tid)["state"] == "new"
        assert attempt(c, tid, "А").status_code == 200
        assert post(c, tid, "hint", level=1).status_code == 200
