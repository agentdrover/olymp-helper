"""Серверное состояние разбора (OH-T06, AC-1, AC-2, AC-4, AC-5, AC-22)."""

import json

from tests.bank_helpers import db
from tests.tutor_helpers import CHILD, PARENT, Scripted, attempt, boot, events, out, post, seed, view

SOLUTION = "SOLUTION-TEXT-ZZZ"
FORBIDDEN_KEYS = {"correct", "is_correct", "answer_key", "key", "teacher_note", "model_answer"}


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, path)


def clean(resp, *, sentinel=SOLUTION):
    """Nothing in this response marks an option as correct or carries the solution."""
    assert sentinel not in resp.text, resp.text
    assert "Решение модели с импорта" not in resp.text
    assert "teacher_note" not in resp.text
    if "json" in resp.headers.get("content-type", ""):
        data = resp.json()
        for where, key in walk(data):
            assert key not in FORBIDDEN_KEYS, where
        assert data.get("solution") is None, data.get("solution")
    else:
        assert "data-correct" not in resp.text
    return resp


def test_ac1(tmp_path):
    def reply(kind, content):
        if kind == "solution":
            return out("Б", SOLUTION)
        return out("Б", "Подумай, какая наука изучает клетку.")

    c, clock, model = boot(tmp_path, Scripted(reply=reply))
    with c:
        tid = seed(tmp_path, key="Б", model_answer=None)
        clean(c.get(f"/tasks/{tid}", headers=CHILD))
        clean(c.get(f"/tasks/{tid}/tutor", headers=CHILD))
        clean(c.get(f"/api/tasks/{tid}/tutor", headers=CHILD))
        clean(c.get(f"/api/tasks/{tid}", headers=CHILD))
        # подсказки 2 и 3 нельзя перескочить ни телом, ни параметром запроса
        for level in (2, 3):
            r = post(c, tid, "hint", level=level)
            assert r.status_code == 409, (level, r.text)
            clean(r)
        for q in ("?level=2", "?level=3"):
            r = c.post(f"/tasks/{tid}/hint{q}", json={}, headers=CHILD)
            assert r.status_code == 409, (q, r.text)
            clean(r)
        # «объясни иначе» до решения отклоняется и не уходит к модели
        r = post(c, tid, "explain", mode="simpler")
        assert r.status_code == 409
        clean(r)
        # неверная попытка с разбором ошибки
        r = clean(attempt(c, tid, "А"))
        assert r.status_code == 200
        assert r.json()["state"] == "attempted_wrong"
        r = clean(post(c, tid, "hint", level=1))
        assert r.status_code == 200 and r.json()["hints"] == 1
        # подсказка 3 до подсказки 2 - отказ, с подтверждением тоже
        r = post(c, tid, "hint", level=3, confirm=True)
        assert r.status_code == 409
        clean(r)
        clean(c.get(f"/tasks/{tid}/tutor", headers=CHILD))
        assert "explain" not in model.kinds() and "solution" not in model.kinds()


def test_ac2(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        a = seed(tmp_path, key="Б")
        r = attempt(c, a, "А").json()
        assert r["state"] == "attempted_wrong" and r["solution"] is None
        r = attempt(c, a, "Б").json()
        assert r["state"] == "solved"
        assert r["solution"]["answer"] == "Б"
        b = seed(tmp_path, key="В")
        attempt(c, b, "А")
        assert post(c, b, "hint", level=1).json()["state"] == "hint1"
        assert post(c, b, "hint", level=2).json()["state"] == "hint2"
        r = post(c, b, "hint", level=3, confirm=True).json()
        assert r["state"] == "hint3"
        assert r["solution"]["answer"] == "В"
        assert view(c, b)["solution"]["answer"] == "В"
        # решённое задание новыми проверками не меняется
        assert attempt(c, b, "А").status_code == 409


def test_ac4(tmp_path):
    def reply(kind, content):
        return out("В", "EXPLAIN-V-ZZZ")

    c, clock, model = boot(tmp_path, Scripted(reply=reply))
    with c:
        tid = seed(tmp_path, key="Б")
        wrong = attempt(c, tid, "А")
        assert "EXPLAIN-V-ZZZ" not in wrong.text and wrong.json()["solution"] is None
        solved = attempt(c, tid, "Б").json()
        sol = solved["solution"]
        assert sol["answer"] == "Б"
        assert "уточняется у родителя" in sol["note"]
        assert sol["explanation"] is None
        assert "EXPLAIN-V-ZZZ" not in json.dumps(solved, ensure_ascii=False)
        conn = db(tmp_path)
        rows = conn.execute("SELECT * FROM tutor_discrepancy WHERE task_item_id = ?", (tid,)).fetchall()
        assert rows, "расхождение не записано родителю"
        assert json.loads(rows[-1]["model_answer"]) == "В"
        assert rows[-1]["explanation"] == "EXPLAIN-V-ZZZ"
        # родитель видит расхождение в данных задания
        parent = c.get(f"/parent/tasks/{tid}", headers=PARENT)
        assert parent.status_code == 200
        # «объясни иначе» по спорному ключу ребёнку объяснения модели не отдаёт
        r = post(c, tid, "explain", mode="simpler")
        assert "EXPLAIN-V-ZZZ" not in r.text


def test_ac5(tmp_path):
    c, clock, model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key=None, model_answer="Б")
        r = attempt(c, tid, "А").json()
        assert r["state"] == "attempted_wrong"
        assert "Б" not in json.dumps({k: v for k, v in r.items() if k != "task"}, ensure_ascii=False)
        r = attempt(c, tid, "Б").json()
        assert r["state"] == "solved_unknown"
        assert r["solution"]["unverified"] is True
        assert "ответ модели, не проверен" in r["solution"]["note"]
        assert "верно" not in r["outcome"].lower()
        html = c.get(f"/tasks/{tid}/tutor", headers=CHILD).text
        assert "ответ модели, не проверен" in html
        data = [e["data"] for e in events(tmp_path, "attempt")]
        assert [d["correctness"] for d in data] == ["unknown", "unknown"]
        conn = db(tmp_path)
        run = conn.execute("SELECT * FROM tutor_run WHERE task_item_id = ?", (tid,)).fetchone()
        assert run["state"] == "solved_unknown" and "не проверено" in run["outcome"]


def test_uncertainty_is_stored_and_validated(tmp_path):
    replies = iter([out("Б", "Подумай про клетку.", "high"), out("Б", "Идея.", "maybe")])
    c, clock, model = boot(tmp_path, Scripted(reply=lambda k, content: next(replies)))
    with c:
        tid = seed(tmp_path, key="Б")
        conn = db(tmp_path)
        before = len(events(tmp_path, "attempt"))
        attempt(c, tid, "А")  # разбор ошибки уходит к модели: тоже сохраняет uncertainty
        mistake = json.loads(
            conn.execute("SELECT analysis_json FROM tutor_run").fetchone()["analysis_json"]
        )
        assert mistake["uncertainty"] == "high"
        assert before == 0
        r = post(c, tid, "hint", level=1)
        # неверное значение uncertainty - ошибка модели: подсказка не выдана и не засчитана
        assert r.json()["panel"]["state"] == "down"
        assert r.json()["hints"] == 0
        assert not events(tmp_path, "hint")
        from app.tutor.prompts import ModelOutputError, parse_output

        assert parse_output(out("Б", "x", "low"), "single").uncertainty == "low"
        for bad in ("medium", None, 1, True, ""):
            try:
                parse_output(out("Б", "x", bad), "single")
            except ModelOutputError:
                pass
            else:
                raise AssertionError(bad)
