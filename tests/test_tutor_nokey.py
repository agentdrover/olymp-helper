"""Задания без ключа, повтор и спорный ключ (OH-T06, AC-19, AC-20)."""

import json

from tests.tutor_helpers import (
    PARENT,
    Scripted,
    attempt,
    boot,
    events,
    out,
    post,
    seed,
)


def test_ac19(tmp_path):
    def reply(kind, content):
        return out("Г", "Г — верный вариант, выбирай его.")

    c, _clock, _model = boot(tmp_path, Scripted(reply=reply))
    with c:
        tid = seed(tmp_path, key=None, model_answer="Б")
        r = attempt(c, tid, "А")
        data = r.json()
        data.pop("task")  # условие и варианты показываются ребёнку в любом случае
        assert "Б" not in json.dumps(data, ensure_ascii=False)
        assert data["solution"] is None
        h = post(c, tid, "hint", level=1).json()
        text = json.dumps([x["html"] for x in h["hints_list"]], ensure_ascii=False)
        assert "верный" not in text and "Г" not in text
        done = attempt(c, tid, "Б").json()
        assert done["state"] == "solved_unknown"
        assert "ответ модели, не проверен" in done["solution"]["note"]


def test_ac20(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        assert post(c, tid, "dispute").status_code == 409  # пока решение не открыто
        assert post(c, tid, "repeat").status_code == 409
        attempt(c, tid, "А", confidence="sure")
        attempt(c, tid, "Б")
        before = events(tmp_path)
        r = post(c, tid, "repeat").json()
        assert r["repeat"] is True and r["state"] == "new" and r["attempts"] == 0
        attempt(c, tid, "Б")
        after = events(tmp_path)
        assert after[: len(before)] == before  # старые события на месте
        new = [e for e in after[len(before) :] if e["type"] == "attempt"]
        assert new and new[0]["data"]["repeat"] is True
        assert all(e["data"]["repeat"] is False for e in before if e["type"] == "attempt")
        d = post(c, tid, "dispute")
        assert d.status_code == 200
        assert events(tmp_path, "disputed_key")[0]["task_item_id"] == tid
        assert events(tmp_path, "disputed_key")[0]["data"]["from_child"] is True
        queue = c.get("/parent/review/queue", headers=PARENT).json()["cards"]
        mine = [x for x in queue if x["task_id"] == tid]
        assert mine and mine[0]["kind"] == "child_dispute"
