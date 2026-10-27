"""OH-K5a: a model-made question is published without the parent only if author, critic (the
OTHER model channel), editor and every code gate agree; otherwise it waits in the parent's feed
with a reason."""

from fastapi.testclient import TestClient

from app.ingest import pipeline
from app.model.types import ModelResult
from tests.bank_helpers import CHILD, PARENT, build_app, db, make_task, text_pdf, upload
from tests.test_generated_packs import BANK, DRAFT_ONLY, NEW, TOPIC, PackModel, good, request_pack

MARK = "составлено помощником, проверено двумя моделями"
Q1, Q2, Q3 = ("хлорофилл", "наследственной информации", "запасает воду")
NOBODY = {"reveals": False, "reason": "нет"}


def by_fragment(table):
    """Handler: answer chosen by the question fragment found in the request text."""

    def handler(content):
        for fragment, answer in table.items():
            if fragment in content:
                return answer
        return None

    return handler


def agree(solution, **extra):
    return {"solution": solution, "issues": [], **extra}


AGREE_ALL = by_fragment({Q1: agree("Г"), Q2: agree("А"), Q3: agree("В")})


class PipeModel(PackModel):
    """PackModel + the second channel: critic, editor and judge are answered by handlers."""

    def __init__(self, bank, pack, critic=AGREE_ALL, editor=None, judge=None):
        super().__init__(bank, pack)
        self.critic = critic
        self.editor = editor
        self.judge = judge or (lambda content: NOBODY)
        self.log = []  # (schema title, channel, request text)

    def complete(self, system, messages, schema=None, session_id=None):
        if (schema or {}).get("title") == "question_pack":
            self.log.append(("question_pack", "claude", messages[-1]["content"]))
        return super().complete(system, messages, schema, session_id)

    def complete_on(self, channel, system, messages, schema=None, session_id=None):
        title = (schema or {}).get("title")
        content = messages[-1]["content"]
        self.log.append((title, channel, content))
        handler = {
            "question_critic": self.critic,
            "question_edit": self.editor,
            "tutor_judge": self.judge,
        }[title]
        payload = handler(content) if callable(handler) else handler
        if isinstance(payload, ModelResult):
            return payload
        if payload is None:
            return ModelResult.failure(channel, "stub", "executor_unreachable")
        return ModelResult(None, payload, channel, "stub", 1, None)

    def channels_of(self, title):
        return [ch for t, ch, _ in self.log if t == title]


def make_pipeline(tmp_path, model_args, **kw):
    tasks = [make_task(n, t, topic=TOPIC) for n, t in enumerate(BANK, 1)]
    tasks.append(make_task(len(BANK) + 1, DRAFT_ONLY, topic=TOPIC))
    model = PipeModel(tasks, model_args, **kw)
    c = TestClient(build_app(tmp_path, model))
    c.__enter__()
    upload(c, text_pdf([[f"{n}. вопрос" for n in range(1, len(tasks) + 1)]]))
    ids = [r["id"] for r in db(tmp_path).execute("SELECT id FROM task_item ORDER BY id")]
    for tid in ids[: len(BANK)]:
        r = c.post(f"/parent/tasks/{tid}/confirm", headers=PARENT, json={"answer": "Б"})
        assert r.status_code == 200, r.text
    return c, model


def rows(tmp_path):
    conn = db(tmp_path)
    try:
        return conn.execute(
            "SELECT * FROM task_item WHERE pack_id IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def unpublished(tmp_path):
    return [r for r in rows(tmp_path) if r["import_status"] == "draft"]


def test_a_question_that_passes_every_gate_is_published_with_the_mark(tmp_path):
    c, model = make_pipeline(tmp_path, {"questions": NEW[:2]})
    with c:
        assert request_pack(c, 2).status_code == 303
        made = rows(tmp_path)
        assert len(made) == 2
        assert {r["import_status"] for r in made} == {"checked"}
        assert {r["key_source"] for r in made} == {"generated"}
        assert {r["auto_checked"] for r in made} == {1}
        # the author is channel A, critic and judge are the OTHER channel, never the same one
        assert model.channels_of("question_pack") == ["claude"]
        assert model.channels_of("question_critic") == ["cursor", "cursor"]
        assert model.channels_of("tutor_judge") == ["cursor", "cursor"]
        assert model.channels_of("question_edit") == []  # nothing to fix, nothing to edit
        # the critic solves WITHOUT the key and without the author's explanation
        for title, _, content in model.log:
            if title == "question_critic":
                assert "answer_key" not in content and "потому что так" not in content
        # the child sees them, with the visible mark
        listed = c.get("/api/tasks", headers=CHILD).json()["tasks"]
        mine = [t for t in listed if t["id"] in {r["id"] for r in made}]
        assert len(mine) == 2 and {t["source"] for t in mine} == {MARK}
        for r in made:
            assert MARK in c.get(f"/tasks/{r['id']}", headers=CHILD).text
        assert MARK in c.get("/tasks", headers=CHILD).text
        # and they are not in the parent's feed
        queue = {
            x["task_id"] for x in c.get("/parent/review/queue", headers=PARENT).json()["cards"]
        }
        assert queue.isdisjoint(r["id"] for r in made)
        pack = db(tmp_path).execute("SELECT * FROM question_pack").fetchone()
        assert pack["published_count"] == 2 and pack["accepted_count"] == 0


def reason_of(task):
    return task["review_note"]


def test_a_disagreement_or_a_failed_gate_goes_to_the_parent(tmp_path):
    exact_copy = good(NEW[1]["text"] + "!", "А")  # the editor will rewrite it into a bank copy
    leaky = good(
        "Какое вещество у растений поглощает свет при фотосинтезе, как сказал учитель?", "Г"
    )
    pack = {"questions": [NEW[0], exact_copy, leaky, NEW[2]]}

    def critic(content):
        if Q1 in content:  # disagrees with the key and the editor does not resolve it
            issue = {"kind": "second_correct", "text": "вариант А тоже верен"}
            return {"solution": "А", "issues": [issue]}
        if Q2 in content:
            return {"solution": "А", "issues": [{"kind": "ambiguous", "text": "мутно"}]}
        if "поглощает свет" in content:
            return agree("Г")
        return agree("В")

    def editor(content):
        if Q1 in content:  # keeps its key
            return {"unfixable": False, "reason": "", "question": NEW[0]}
        if Q2 in content:  # rewrites the question into a normalized copy of a bank question
            copy = good(BANK[0].upper().replace("?", " ?"), "А")
            return {"unfixable": False, "reason": "", "question": copy}
        return None

    def judge(content):
        if "поглощает свет" in content:
            return {"reveals": True, "reason": "условие намекает на вариант Г"}
        return NOBODY

    c, _ = make_pipeline(tmp_path, pack, critic=critic, editor=editor, judge=judge)
    with c:
        request_pack(c, 4)
        made = rows(tmp_path)
        by_text = {r["text"]: r for r in made}
        # only the clean one is published
        assert [r["import_status"] for r in made].count("checked") == 1
        disagree = by_text[NEW[0]["text"]]
        assert disagree["import_status"] == "draft" and disagree["auto_checked"] == 0
        assert "критик" in reason_of(disagree) and "вариант А тоже верен" in reason_of(disagree)
        dup = next(
            r
            for r in made
            if "ТЕРМИН" not in r["text"]
            and r["text"].upper().startswith("КАКОЙ ОРГАНОИД КЛЕТКИ ОТВЕЧАЕТ")
        )
        assert dup["import_status"] == "draft" and "повтор" in reason_of(dup)
        leak = next(r for r in made if "поглощает свет" in r["text"])
        assert leak["import_status"] == "draft" and "условие намекает на вариант Г" in reason_of(
            leak
        )
        assert by_text[NEW[2]["text"]]["import_status"] == "checked"
        # drafts wait in the parent's feed and the child cannot reach them
        feed = {x["task_id"] for x in c.get("/parent/review/queue", headers=PARENT).json()["cards"]}
        for r in (disagree, dup, leak):
            assert r["id"] in feed
            assert c.get(f"/tasks/{r['id']}", headers=CHILD).status_code == 404


def test_a_topic_outside_the_catalog_is_not_published(tmp_path):
    model = PipeModel([], None)
    from app.ingest.packs import check_item

    task = check_item(NEW[0], "single")
    run = pipeline.PackRun(model, "claude")
    verdict = pipeline.vet_item(run, task, "single", "b99.nowhere", {TOPIC: {"title": "t"}}, set())
    assert not verdict.published and "каталог" in verdict.reason
    assert model.log == []  # a code gate: no model is asked


def test_a_critic_that_is_unavailable_or_invalid_sends_the_question_to_the_parent(tmp_path):
    for broken in (
        None,
        {"solution": "Г"},
        {"solution": 5, "issues": []},
        "не json",
        {"solution": "Г", "issues": [{"kind": "odd", "text": "x"}]},
    ):
        sub = tmp_path / str(abs(hash(str(broken))))
        c, model = make_pipeline(sub, {"questions": NEW[:1]}, critic=lambda content, b=broken: b)
        with c:
            request_pack(c, 1)
            (task,) = rows(sub)
            assert task["import_status"] == "draft", broken
            assert "критик недоступен" in reason_of(task)
            assert model.channels_of("tutor_judge") == []


def test_when_the_daily_limit_is_hit_the_rest_of_the_pack_goes_to_the_parent(tmp_path):
    def critic(content):
        if Q1 in content:
            return agree("Г")
        return ModelResult.failure("cursor", "stub", "limit")

    c, model = make_pipeline(tmp_path, {"questions": NEW}, critic=critic)
    with c:
        request_pack(c, 3)
        made = rows(tmp_path)
        assert [r["import_status"] for r in made] == ["checked", "draft", "draft"]
        assert "лимит" in reason_of(made[1]) and "лимит" in reason_of(made[2])
        # the stopped pack asks nobody else: one critic hit the limit, the third was not asked
        assert model.channels_of("question_critic") == ["cursor", "cursor"]
        assert len(model.channels_of("tutor_judge")) == 1


def test_an_editor_that_fixes_the_question_gets_it_published_in_its_final_form(tmp_path):
    fixed = good("Какой органоид растительной клетки содержит хлорофилл и ведёт фотосинтез?", "Г")

    def critic(content):
        return {"solution": "Г", "issues": [{"kind": "level", "text": "слишком сложно"}]}

    c, model = make_pipeline(
        tmp_path,
        {"questions": NEW[:1]},
        critic=critic,
        editor={"unfixable": False, "reason": "", "question": fixed},
    )
    with c:
        request_pack(c, 1)
        (task,) = rows(tmp_path)
        assert task["import_status"] == "checked" and task["text"] == fixed["text"]
        assert model.channels_of("question_edit") == ["claude"]  # channel A edits
        judged = [x for x in model.log if x[0] == "tutor_judge"]
        assert fixed["text"] in judged[0][2]  # the judge saw the FINAL text


def test_an_unfixable_question_goes_to_the_parent(tmp_path):
    c, _ = make_pipeline(
        tmp_path,
        {"questions": NEW[:1]},
        critic=lambda content: {
            "solution": "Г",
            "issues": [{"kind": "factual_error", "text": "ошибка"}],
        },
        editor={"unfixable": True, "reason": "нельзя исправить", "question": None},
    )
    with c:
        request_pack(c, 1)
        (task,) = rows(tmp_path)
        assert task["import_status"] == "draft"
        assert "нельзя исправить" in reason_of(task) and "ошибка" in reason_of(task)


def test_the_prefilter_stops_a_leak_before_the_judge_is_asked(tmp_path):
    leaky = good("Подскажу: правильный ответ Г, а теперь назови органоид с хлорофиллом?", "Г")
    c, model = make_pipeline(
        tmp_path, {"questions": [leaky]}, critic=by_fragment({"хлорофилл": agree("Г")})
    )
    with c:
        request_pack(c, 1)
        made = rows(tmp_path)
        # it either never gets past the structure check or is stopped by the prefilter
        assert all(r["import_status"] == "draft" for r in made)
        assert model.channels_of("tutor_judge") == []


def test_an_oversized_request_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_REQUEST_BYTES", 10)
    c, model = make_pipeline(tmp_path, {"questions": NEW[:1]})
    with c:
        request_pack(c, 1)
        (task,) = rows(tmp_path)
        assert task["import_status"] == "draft" and "велик" in reason_of(task)
        assert model.channels_of("question_critic") == []


def test_a_parent_edit_or_confirmation_replaces_the_two_model_mark(tmp_path):
    c, _ = make_pipeline(tmp_path, {"questions": NEW[:1]})
    with c:
        request_pack(c, 1)
        (task,) = rows(tmp_path)
        r = c.post(f"/parent/tasks/{task['id']}/confirm", headers=PARENT, json={"answer": "А"})
        assert r.status_code == 200, r.text
        page = c.get(f"/tasks/{task['id']}", headers=CHILD).text
        assert "проверено родителем" in page and MARK not in page
        assert (
            db(tmp_path)
            .execute("SELECT auto_checked FROM task_item WHERE id = ?", (task["id"],))
            .fetchone()[0]
            == 0
        )


def test_the_pack_page_counts_published_and_waiting(tmp_path):
    c, _ = make_pipeline(
        tmp_path, {"questions": NEW[:2]}, critic=by_fragment({Q1: agree("Г"), Q2: agree("Б")})
    )
    with c:
        r = request_pack(c, 2)
        page = c.get(r.headers["location"], headers=PARENT).text
        assert "без вашей проверки (прошло двух моделей и проверки кода): <b>1</b>" in page
        assert "Принято в ленту проверки: <b>1</b>" in page
