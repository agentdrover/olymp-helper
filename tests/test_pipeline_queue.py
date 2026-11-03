"""OH-K5c: the parent's queue holds what failed the pipeline (with the reason) and a sample of what
passed (every 5th, every 10th after 20 clean checks); a published task can be withdrawn; the
pipeline journal counts passed, rejected and the error rate in the sample."""

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from app import db as schema
from app.ingest import packs, pipeline, sampling
from app.ingest.packs import Verdict, check_item
from app.ingest.store import now
from tests.bank_helpers import CHILD, PARENT, build_app, db
from tests.summary_helpers import attempt, boot_summary, finish, seed, summary_of
from tests.test_generated_packs import NEW, TOPIC
from tests.test_pipeline_generated import PipeModel

DISAGREE = "критик решил иначе: ответ «А», ключ «Б»"
DUPLICATE = "повтор: такое условие уже есть в банке заданий"
LETTERS = str.maketrans("0123456789", "абвгдежзик")


def question(n: int) -> dict:
    """A valid question whose words differ from every other question (no repeat gate hits)."""
    task = check_item(NEW[0], "single")
    task["text"] = "Какой термин " + " ".join(f"{n:03d}{i}".translate(LETTERS) for i in range(6))
    task["number"] = n
    return task


def land(tmp_path, outcomes: list) -> list[int]:
    """Write one pack the way the pipeline does. An outcome is True (passed) or a (gate, reason)
    pair (failed). Returns the new task ids in order."""
    conn = db(tmp_path)
    try:
        before = {r[0] for r in conn.execute("SELECT id FROM task_item")}
        pack = conn.execute(
            "INSERT INTO question_pack (created_at, topic, task_type, requested_count) "
            "VALUES (?, ?, 'single', ?)",
            (now(), TOPIC, min(max(len(outcomes), 1), 20)),
        ).lastrowid
        vetted = []
        for n, outcome in enumerate(outcomes, 1 + len(before)):
            task = question(n)
            if outcome is True:
                vetted.append((task, Verdict(True, task)))
            else:
                gate, reason = outcome
                vetted.append((task, Verdict(False, task, reason, gate)))
        packs._write(conn, pack, TOPIC, vetted, [], "codex")
        return [
            r[0] for r in conn.execute("SELECT id FROM task_item ORDER BY id") if r[0] not in before
        ]
    finally:
        conn.close()


def passed_only(ids_by_outcome, outcomes):
    return [i for i, o in zip(ids_by_outcome, outcomes, strict=True) if o is True]


def queue(c) -> list[dict]:
    r = c.get("/parent/review/queue", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.json()["cards"]


def kinds(c) -> dict:
    out: dict = {}
    for card in queue(c):
        kind = "pipeline_failed" if card["pipeline_failed"] else card["kind"]
        out.setdefault(kind, []).append(card["task_id"])
    return out


def judge(c, task_id, found_error=False):
    r = c.post(f"/parent/tasks/{task_id}/sample", headers=PARENT, json={"found_error": found_error})
    assert r.status_code == 200, r.text
    return r.json()


def journal(c) -> dict:
    r = c.get("/parent/review/journal", headers=PARENT)
    assert r.status_code == 200, r.text
    return r.json()


def boot(tmp_path):
    c = TestClient(build_app(tmp_path, None))
    c.__enter__()
    return c


def test_the_parent_sees_failures_and_a_sample(tmp_path):
    c = boot(tmp_path)
    with c:
        # 25 passed and 2 failed, in one pack
        outcomes = [True] * 25
        outcomes.insert(2, ("disagree", DISAGREE))
        outcomes.insert(17, ("duplicate", DUPLICATE))
        ids = land(tmp_path, outcomes)
        passed = passed_only(ids, outcomes)
        failed = [i for i in ids if i not in passed]
        found = kinds(c)
        # both failures with the reason, and every 5th passed one (5, 10, 15, 20, 25)
        assert found["pipeline_failed"] == failed
        assert found["sample"] == passed[4::5] and len(found["sample"]) == 5
        assert set(found) == {"pipeline_failed", "sample"}
        cards = {card["task_id"]: card for card in queue(c)}
        assert cards[failed[0]]["reason"] == DISAGREE and cards[failed[0]]["gate"] == "disagree"
        assert cards[failed[1]]["reason"] == DUPLICATE and cards[failed[1]]["gate"] == "duplicate"
        assert cards[failed[0]]["label"] == "не прошло конвейер"
        assert cards[found["sample"][0]]["label"] == "выборка прошедших"
        assert journal(c)["sample"]["every"] == 5

        # the parent checks the five and finds nothing: 5 clean checks, still every 5th
        for tid in found["sample"]:
            judge(c, tid)
        assert kinds(c) == {"pipeline_failed": failed}
        assert journal(c)["sample"]["clean_streak"] == 5
        assert journal(c)["sample"]["every"] == 5

        # 75 more passed (every 5th again): checked with no error, 20 clean checks in a row
        more = land(tmp_path, [True] * 75)
        picked = kinds(c)["sample"]
        assert picked == more[4::5] and len(picked) == 15
        for tid in picked:
            judge(c, tid)
        assert journal(c)["sample"]["clean_streak"] == 20
        assert journal(c)["sample"]["every"] == 10

        # now every 10th: 20 passed give two samples, not four
        later = land(tmp_path, [True] * 20)
        picked = kinds(c)["sample"]
        assert picked == later[9::10] and len(picked) == 2

        # a found error returns the sample to every 5th
        judge(c, picked[0], found_error=True)
        state = journal(c)["sample"]
        assert state["every"] == 5 and state["clean_streak"] == 0 and state["errors"] == 1
        after = land(tmp_path, [True] * 10)
        assert [t for t in kinds(c)["sample"] if t in after] == after[4::5]
        assert len([t for t in kinds(c)["sample"] if t in after]) == 2

        # the child never sees a failed question; the sampled passed ones stay published
        listed = {t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]}
        assert listed.isdisjoint(failed) and set(passed) <= listed


def test_a_withdrawn_task_disappears_but_its_history_stays(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б", number="7")
        assert tid in [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]]
        assert attempt(c, tid, "А").status_code == 200
        assert finish(c).status_code == 200
        sid = c.get("/api/my", headers=CHILD).json()["sessions"][0]["id"]
        before = summary_of(c, sid)
        assert before["tasks"][0]["withdrawn"] is False
        assert "задание снято" not in c.get("/my", headers=CHILD).text

        r = c.post(f"/parent/tasks/{tid}/withdraw", headers=PARENT)
        assert r.status_code == 200, r.text

        # the child no longer sees it: list, card, search, attempts
        assert tid not in [t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]]
        assert c.get(f"/api/tasks/{tid}", headers=CHILD).status_code == 404
        assert c.get(f"/tasks/{tid}", headers=CHILD).status_code == 404
        assert attempt(c, tid, "Б").status_code == 404
        # the answers stay in the results, marked
        after = summary_of(c, sid)
        (entry,) = after["tasks"]
        assert entry["withdrawn"] is True
        assert [a["answer"] for a in entry["attempts"]] == ["А"]
        assert entry["outcome"] == before["tasks"][0]["outcome"]
        page = c.get("/my", headers=CHILD).text
        assert "задание снято" in page
        assert f'href="/tasks/{tid}"' not in page
        assert "задание снято" in c.get(f"/parent/summaries/{sid}", headers=PARENT).text
        # a withdrawn task is not in the parent's queue either; a second withdrawal is refused
        assert tid not in [card["task_id"] for card in queue(c)]
        assert c.post(f"/parent/tasks/{tid}/withdraw", headers=PARENT).status_code == 409


def test_the_journal_counts_passed_rejected_and_the_sample_errors(tmp_path):
    c = boot(tmp_path)
    with c:
        assert journal(c)["passed"] == 0 and journal(c)["rejected"] == 0
        assert journal(c)["sample"]["error_rate"] is None
        outcomes = [True] * 10 + [("disagree", DISAGREE)] * 2 + [("duplicate", DUPLICATE)]
        ids = land(tmp_path, outcomes)
        data = journal(c)
        assert data["passed"] == 10 and data["rejected"] == 3
        assert {r["gate"]: r["count"] for r in data["reasons"]} == {"disagree": 2, "duplicate": 1}
        assert {r["label"] for r in data["reasons"]} == {
            "критик решил иначе, чем ключ",
            "повтор вопроса из банка",
        }
        picked = kinds(c)["sample"]
        assert len(picked) == 2 and data["sample"]["pending"] == 2
        judge(c, picked[0])
        judge(c, picked[1], found_error=True)
        sample = journal(c)["sample"]
        assert (sample["reviewed"], sample["errors"], sample["pending"]) == (2, 1, 0)
        assert sample["error_rate"] == 0.5
        # the journal is on the parent's feed page
        page = c.get("/parent/review", headers=PARENT).text
        assert "Журнал конвейера" in page and "критик решил иначе, чем ключ" in page
        # the failure reasons of the pipeline are named by the real gates
        task = check_item(NEW[0], "single")
        run = pipeline.PackRun(PipeModel([], None), "codex")
        verdict = pipeline.vet_item(run, task, "single", "b99.x", {TOPIC: {"title": "t"}}, set())
        assert (verdict.published, verdict.gate) == (False, "topic")
        assert len(ids) == 13


def test_a_parents_edit_exclusion_or_withdrawal_of_a_sampled_task_is_a_found_error(tmp_path):
    c = boot(tmp_path)
    with c:
        ids = land(tmp_path, [True] * 15)
        sampled = ids[4::5]
        assert kinds(c)["sample"] == sampled
        edit = c.post(f"/parent/tasks/{sampled[0]}/edit", headers=PARENT, json={"text": "Новое?"})
        assert edit.status_code == 200, edit.text
        assert c.post(f"/parent/tasks/{sampled[1]}/exclude", headers=PARENT).status_code == 200
        assert c.post(f"/parent/tasks/{sampled[2]}/withdraw", headers=PARENT).status_code == 200
        sample = journal(c)["sample"]
        assert (sample["reviewed"], sample["errors"], sample["pending"]) == (3, 3, 0)
        # the edited one is a draft for the parent now, the others are gone from the queue
        assert kinds(c) == {"confirm": [sampled[0]]}
        assert sampled[0] not in {
            t["id"] for t in c.get("/api/tasks", headers=CHILD).json()["tasks"]
        }


def test_only_a_pending_sample_can_be_judged(tmp_path):
    c = boot(tmp_path)
    with c:
        ids = land(tmp_path, [True] * 5 + [("leak", "в условии очевидный признак ответа (х)")])
        picked, failed = ids[4], ids[5]
        other = ids[0]  # published, but not in the sample
        for tid in (other, failed):
            r = c.post(f"/parent/tasks/{tid}/sample", headers=PARENT, json={})
            assert r.status_code == 409, tid
        assert c.post("/parent/tasks/9999/sample", headers=PARENT, json={}).status_code == 404
        assert c.post(f"/parent/tasks/{failed}/withdraw", headers=PARENT).status_code == 409
        judge(c, picked)
        assert c.post(f"/parent/tasks/{picked}/sample", headers=PARENT, json={}).status_code == 409
        # a failed question is an ordinary draft card: confirming it publishes it, off the queue
        card = next(x for x in queue(c) if x["task_id"] == failed)
        assert card["button"] and "Конвейер не пропустил" in card["hint"]
        assert c.post(f"/parent/tasks/{failed}/confirm", headers=PARENT, json={}).status_code == 200
        assert failed not in [x["task_id"] for x in queue(c)]


def test_every_gate_a_stop_names_has_a_label():
    tree = ast.parse(Path(pipeline.__file__).read_text(encoding="utf-8"))
    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Stop":
            gate = node.args[1] if len(node.args) > 1 else None
            assert gate is not None, ast.dump(node)[:80]  # every Stop names its gate
            named |= {n.value for n in ast.walk(gate) if isinstance(n, ast.Constant)}
    assert {"topic", "disagree", "remarks", "editor", "duplicate", "leak", "judge"} <= named
    assert named <= set(sampling.GATES)


def test_the_migration_marks_tasks_that_already_went_through_the_pipeline(tmp_path, monkeypatch):
    files = schema._migration_files()
    conn = schema.connect(tmp_path / "olymp.db")
    try:
        monkeypatch.setattr(schema, "_migration_files", lambda: [f for f in files if f[0] <= 18])
        assert schema.migrate(conn) == 18
        conn.execute(
            "INSERT INTO question_pack (id, created_at, topic, task_type, requested_count) "
            "VALUES (1, 'now', 'b10.cell', 'single', 3)"
        )
        rows = [
            (1, "checked", 1, None),  # passed the pipeline
            (2, "draft", 0, "Сгенерировано. Не опубликовано без вас: критик решил иначе."),
            (3, "checked", 0, None),  # confirmed by the parent before the pipeline existed
        ]
        for tid, status, auto, note in rows:
            conn.execute(
                "INSERT INTO task_item (id, type, text, import_status, answer_key, key_source, "
                "review_note, pack_id, auto_checked) "
                "VALUES (?, 'single', ?, ?, '{\"value\": \"Б\"}', 'generated', ?, 1, ?)",
                (tid, f"Вопрос {tid}", status, note, auto),
            )
        monkeypatch.setattr(schema, "_migration_files", lambda: files)
        assert schema.migrate(conn) == schema.SCHEMA_VERSION
        got = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT id, pipeline_outcome, pipeline_gate, pipeline_reason, sample_state, "
                "withdrawn_at FROM task_item"
            )
        }
        assert got[1] == ("passed", None, None, None, None)
        assert got[2] == ("failed", "other", "критик решил иначе", None, None)
        assert got[3] == (None, None, None, None, None)
    finally:
        conn.close()


def test_an_error_found_in_an_accepted_sample_resets_the_streak(tmp_path):
    c = boot(tmp_path)
    with c:
        ids = land(tmp_path, [True] * 10)
        first, second = ids[4], ids[9]
        judge(c, first)
        judge(c, second)
        assert journal(c)["sample"]["clean_streak"] == 2
        # published without the parent, whether sampled or not, the parent can withdraw them
        listed = c.get("/parent/review/auto/list", headers=PARENT).text
        for tid in ids:
            assert f'data-withdraw="{tid}"' in listed
        # the error in an already accepted sample is found later: it counts as an error
        assert c.post(f"/parent/tasks/{first}/withdraw", headers=PARENT).status_code == 200
        sample = journal(c)["sample"]
        assert (sample["reviewed"], sample["errors"], sample["clean_streak"]) == (2, 1, 0)
        assert (
            f'data-withdraw="{first}"' not in c.get("/parent/review/auto/list", headers=PARENT).text
        )
        # a second withdrawal is refused and does not count twice
        assert c.post(f"/parent/tasks/{first}/withdraw", headers=PARENT).status_code == 409


def test_the_parent_can_withdraw_any_published_task_from_one_page(tmp_path):
    c, _clock, _model = boot_summary(tmp_path)
    with c:
        mine = seed(tmp_path, key="Б", number="3")  # confirmed by the parent, no pipeline
        made = land(tmp_path, [True, ("leak", "в условии очевидный признак ответа (х)")])
        page = c.get("/parent/published", headers=PARENT).text
        assert f'data-withdraw="{mine}"' in page and f'data-withdraw="{made[0]}"' in page
        assert f'data-withdraw="{made[1]}"' not in page  # a failed question is not published
        assert "/static/withdraw.js" in page
        assert c.get("/parent/published", headers=CHILD).status_code in (302, 303, 401, 403)
        assert c.post(f"/parent/tasks/{mine}/withdraw", headers=PARENT).status_code == 200
        assert f'data-withdraw="{mine}"' not in c.get("/parent/published", headers=PARENT).text
