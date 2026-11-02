"""«Помоги задать вопрос», the editable voice text, the shared panel state and the panel's place
on the task screen (OH-T07, SPEC 1.3 rule 6 and 8, 1.3a)."""

import re
import threading

from tests.bank_helpers import CHILD
from tests.chat_helpers import Recognizer, boot_chat, chat_reply, say, voice_post
from tests.tutor_helpers import Blocking, Scripted, events, seed

QUESTION = "Чем отличается митохондрия от хлоропласта?"


def page(c, tid) -> str:
    r = c.get(f"/tasks/{tid}", headers=CHILD)
    assert r.status_code == 200
    return r.text


def test_ac7(tmp_path):
    model = Scripted(
        default=chat_reply(
            "Что именно непонятно: названия или работа органоидов?", suggestions=[QUESTION]
        )
    )
    c, model, _v, _a = boot_chat(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        r = say(c, tid, "про органоиды не понимаю", mode="formulate")
        assert r.status_code == 200
        # the ready wording is offered for the input field ...
        assert r.json()["compose"] == [QUESTION]
        # ... and nothing was sent for the child: one model call, one exchange, no answer to it
        assert model.kinds() == ["formulate"]
        sent = events(tmp_path, "chat")
        assert len(sent) == 1 and sent[0]["data"]["mode"] == "formulate"
        assert sent[0]["data"]["question"] == "про органоиды не понимаю"
        # the helper does not answer on the substance in this mode: the request says so
        content = model.calls[0]["content"]
        assert "Режим: formulate\n" in content and "НЕ отвечай по существу" in content

        # «отправить в разбор»: the edited wording goes at once, as a question about the task
        r2 = say(c, tid, QUESTION + " Объясни на примере.", mode="chat")
        assert r2.status_code == 200 and r2.json()["compose"] == []
        assert model.kinds() == ["formulate", "chat"]
        assert "Режим: chat\n" in model.calls[1]["content"]
        assert "Задание №" in model.calls[1]["content"]
        assert [e["data"]["mode"] for e in events(tmp_path, "chat")] == ["formulate", "chat"]

        # the screen: a field, the insert buttons and the send button live in the panel
        html = page(c, tid)
        assert 'id="chat-input"' in html and "Отправить в разбор" in html
        assert 'data-chat-mode="formulate"' in html and "Помоги задать вопрос" in html
        frag = c.post(
            f"/tasks/{tid}/chat",
            json={"message": "про органоиды", "mode": "formulate"},
            headers={**CHILD, "HX-Request": "true"},
        )
        assert "data-insert" in frag.text and QUESTION in frag.text  # a button that inserts
        assert 'data-chat-mode="chat"' in html

    # a wording the child never sees on its own: the model's suggestion is not a message
    c, model, _v, _a = boot_chat(tmp_path / "x", Scripted(default=chat_reply("Ок", ["x"])))
    with c:
        tid = seed(tmp_path / "x", key="Б")
        assert say(c, tid, "   ").status_code == 422  # an empty question is not sent
        assert model.calls == []


def test_ac9(tmp_path):
    rec = Recognizer("чем отличается клетка от ткани")
    c, model, _v, _a = boot_chat(tmp_path, recognizer=rec)
    with c:
        tid = seed(tmp_path, key="Б")
        # recognized text comes back for the input field; it is NOT sent to the model
        r = voice_post(c, tid)
        assert r.json() == {"ok": True, "text": "чем отличается клетка от ткани"}
        assert model.calls == [] and events(tmp_path, "chat") == []
        # the child edits it and sends it: the edited text is what the model gets
        edited = "чем отличается клетка от ткани у растений"
        assert say(c, tid, edited).status_code == 200
        assert edited in model.calls[0]["content"]
        assert events(tmp_path, "chat")[0]["data"]["question"] == edited

        html = page(c, tid)
        assert re.search(r"<textarea[^>]*id=\"chat-input\"", html)  # the field stays editable
        for text in (
            "Нет доступа к микрофону",
            "Страница открыта не по HTTPS",
            "Распознаю…",
            "Запись длиннее 30 с",
        ):
            assert text in html, text
        assert 'data-voice-state="no_mic"' in html and 'data-voice-state="insecure"' in html
        assert 'data-voice-state="recognizing"' in html and 'data-voice-state="too_long"' in html
        assert 'id="voice-record"' in html


def test_ac10(tmp_path):
    model = Blocking(default=chat_reply("Ответ."))
    model.release.set()
    c, model, _v, _a = boot_chat(tmp_path, model)
    with c:
        tid = seed(tmp_path, key="Б")
        model.release.clear()
        model.entered.clear()
        first = {}
        t = threading.Thread(target=lambda: first.update(r=say(c, tid, "первый")), daemon=True)
        t.start()
        assert model.entered.wait(10)
        # a second question while the model answers: not sent, the shared panel says «думаю…»
        second = say(c, tid, "второй")
        assert second.status_code == 200
        panel = second.json()["panel"]
        assert (panel["state"], panel["text"], panel["busy"]) == ("thinking", "Думаю… 0 с", True)
        assert panel["elapsed_s"] == 0 and panel["step"]
        frag = c.post(
            f"/tasks/{tid}/chat",
            json={"message": "третий", "mode": "chat"},
            headers={**CHILD, "HX-Request": "true"},
        )
        assert 'data-panel="thinking"' in frag.text and "Думаю… 0 с" in frag.text
        assert "disabled" in frag.text  # the panel's buttons are blocked
        # the same Inflight lock covers hints: one model request per task
        assert c.post(f"/tasks/{tid}/hint", json={"level": 1}, headers=CHILD).status_code in (
            200,
            409,
        )
        assert model.calls == []  # nothing else reached the model while it was busy
        model.release.set()
        t.join(10)
        assert first["r"].status_code == 200
        assert [e["data"]["question"] for e in events(tmp_path, "chat")] == ["первый"]
        assert len(model.calls) == 1

        # the same gates as the other child POSTs: same origin, JSON, notice read
        url = f"/tasks/{tid}/chat"
        body = {"message": "привет", "mode": "chat"}
        assert c.post(url, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post(url, data=body).status_code == 415
        assert len(model.calls) == 1
    c2, model2, _v, _a = boot_chat(tmp_path / "n", notice=False)
    with c2:
        tid = seed(tmp_path / "n", key="Б")
        r = say(c2, tid, "привет")
        assert r.status_code == 403 and r.json()["detail"]["error"] == "notice_required"
        assert model2.calls == []


def test_ask_lives_in_the_task_panel_not_in_navigation(tmp_path):
    c, _model, _v, _a = boot_chat(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        for url in ("/today", "/tasks", f"/tasks/{tid}"):
            html = c.get(url, headers=CHILD).text
            nav = re.search(r"<nav.*?</nav>", html, re.DOTALL).group(0)
            assert "Спросить" not in nav and "Спроси" not in nav, url
            for path in ("/ask", "/chat", "/voice"):
                assert f'href="{path}"' not in nav, (url, path)
        for path in ("/ask", "/chat", "/voice"):
            assert c.get(path, headers=CHILD).status_code == 404
        # chat, voice and the formulating mode are inside the helper panel on the task screen
        panel = re.search(r'<aside class="helper".*?</aside>', page(c, tid), re.DOTALL).group(0)
        for marker in ('id="chat-input"', 'id="voice-record"', 'data-chat-mode="formulate"'):
            assert marker in panel, marker
