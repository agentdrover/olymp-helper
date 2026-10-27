"""Чат и «помоги задать вопрос» (SPEC-1.0 1.3 правила 3, 6, 8, 9; 1.5; 1.6).

Это тот же Flow, что у разбора: тот же замок `Inflight` (один запрос к модели на задание), те же
состояния панели, тот же фильтр утечек (`_check_text`: дешёвый фильтр и независимый судья на
ДРУГОМ канале), одна перегенерация, затем общий текст; судья недоступен - текст модели до решения
не показывается вовсе. Своего второго механизма утечек здесь нет.

Готовые формулировки режима «помоги задать вопрос» возвращаются в `compose` и вставляются в поле
ввода на странице; сервер их за ребёнка не отправляет.
"""

import json

from app.chat import store
from app.chat.prompts import (
    CHAT_SCHEMA,
    GENERIC,
    MAX_CITATIONS,
    MAX_MESSAGE,
    MODES,
    ChatOutput,
    ReplyTooLong,
    build_chat_request,
    parse_chat,
)
from app.ingest.publish import assist_mode
from app.ingest.store import atomic, get_task
from app.model.client import MAX_REQUEST_BYTES, request_size
from app.search.textbook import search_textbooks
from app.tutor import state
from app.tutor.flow import LONG_TALK, Flow
from app.tutor.guard import plain, prefilter_reason, strip_claims, strip_markup
from app.tutor.panel import failure_state
from app.tutor.prompts import ModelOutputError
from app.tutor.state import Refused

TOO_LONG = object()  # ответ модели длиннее MAX_REPLY
OVERFLOW = object()  # запрос не помещается в лимит исполнителя


class ChatFlow(Flow):
    def chat(self, task_id: int, message, mode) -> None:
        if mode not in MODES:
            raise Refused(422, "bad_mode", "Такого режима нет.")
        message = " ".join(str(message or "").split())
        if not message:
            raise Refused(422, "empty_message", "Напиши вопрос, потом отправляй.")
        if len(message) > MAX_MESSAGE:
            raise Refused(422, "long_message", f"Вопрос длиннее {MAX_MESSAGE} знаков: сократи его.")
        task = get_task(self.conn, task_id)
        if assist_mode(task) == "key_only":
            self.panel_state = "no_hints"  # как у разбора: без подсказок модель не зовётся
            return
        if not self._begin(task):
            return
        try:
            run = state.active_run(self.conn, task, self.now)
            self._exchange(task, run, mode, message)
        finally:
            self._end(task)

    # ------------------------------------------------------------------ обмен

    def _generate(
        self, task, run, mode, message, history, fragments, opened, feedback=None, shorter=False
    ):
        """ChatOutput, None (panel_state назван), TOO_LONG (ответ длиннее лимита) или OVERFLOW
        (запрос не помещается в лимит исполнителя: модель не зовётся)."""
        system, messages = build_chat_request(
            task, mode, message,
            history=history, fragments=fragments, use_note=True,
            opened_answer=opened, feedback=feedback, shorter=shorter,
        )  # fmt: skip
        if request_size(system, messages, CHAT_SCHEMA) > MAX_REQUEST_BYTES:
            self.notice = LONG_TALK
            return OVERFLOW
        try:
            result = self.model.complete(system, messages, CHAT_SCHEMA, session_id=self.session)
        except Exception:  # noqa: BLE001 - модель любого вида: ребёнку «не отвечает»
            self.panel_state = "down"
            return None
        self._exam_check(task)
        if not getattr(result, "ok", False):
            self.panel_state = failure_state(result)
            return None
        self.channel = getattr(result, "channel", None)
        payload = result.json if getattr(result, "json", None) is not None else result.text
        try:
            return parse_chat(payload)
        except ReplyTooLong:
            return TOO_LONG
        except ModelOutputError:
            self.panel_state = "down"
            return None

    def _exchange(self, task, run, mode: str, message: str) -> None:
        with state.atomic(self.conn):
            self.session = state.session_id_for(self.conn, self.now)
        history = store.history_for_model(self.conn, self.session, task["id"], run["id"])
        fragments = []
        if mode == "chat":
            fragments = search_textbooks(self.conn, f"{message} {task['text']}")
        opened = self._opened_answer(run)
        args = (task, run, mode, message, history, fragments, opened)
        out = self._generate(*args)
        if out is TOO_LONG:  # один раз просим короче, потом общий текст
            out = self._generate(*args, shorter=True)
        if out is None:
            return
        if not isinstance(out, ChatOutput):
            self._store(
                task,
                run,
                mode,
                message,
                GENERIC[mode],
                [],
                [],
                "overflow" if out is OVERFLOW else "too_long",
            )
            return
        reply, suggestions, citations, reason = out.reply, out.suggestions, [], None
        if opened is None:
            for attempt in (0, 1):
                citations = self._citations(task, out, fragments, reveal=False)
                reason, feedback = self._check_visible(task, run, mode, out, citations)
                if reason and citations and reason != "judge_unavailable":
                    # the quotes may be what gives the answer away: try the same text without them
                    bare, _ = self._check_visible(task, run, mode, out, [])
                    if bare is None:
                        citations, reason = [], None
                if reason is None or attempt == 1 or reason == "judge_unavailable":
                    break
                again = self._generate(*args, feedback=feedback)
                if not isinstance(again, ChatOutput):
                    break
                out = again
            reply, suggestions = out.reply, out.suggestions
        else:
            reply, reason = self._open_text(task, reply)
            citations = self._citations(task, out, fragments, reveal=True)
        if reason:
            reply, suggestions, citations = GENERIC[mode], [], []
        else:
            suggestions = [strip_markup(s) for s in suggestions]
        self._store(task, run, mode, message, reply, suggestions, citations, reason)

    def _check_visible(self, task, run, mode, out, citations) -> tuple[str | None, str]:
        """The leak check sees the COMPLETE visible response: reply, wordings and every quote."""
        text = "\n".join([out.reply, *out.suggestions, *(c["quote"] for c in citations)])
        return self._check_text(task, run, text, mode, ())

    def _open_text(self, task, reply: str) -> tuple[str, str | None]:
        """Решение открыто: ответ называть можно; без ключа утверждения о верности вырезаются."""
        _html, reason = self._safe(task, reply, "chat", reveal=True)
        if reason is not None:
            return GENERIC["chat"], reason
        return (reply if self._args(task)[1] else strip_claims(reply)), None

    @staticmethod
    def _opened_answer(run) -> str | None:
        if run is None or not state.is_terminal(run) or not run["solution_json"]:
            return None
        return str(json.loads(run["solution_json"]).get("answer") or "") or None

    def _citations(self, task, out: ChatOutput, fragments, *, reveal: bool) -> list[dict]:
        """Только фрагменты, которые выдал поиск; неизвестные id отбрасываются. Цитата - текст
        найденного фрагмента, не слова модели. До решения цитата проходит тот же дешёвый фильтр."""
        ref, _keyed, options, _note = self._args(task)
        by_id = {f["id"]: f for f in fragments}
        picked, seen = [], set()
        for fid in out.citations:
            frag = by_id.get(fid)
            if frag is None or fid in seen:
                continue
            seen.add(fid)
            if not reveal and prefilter_reason(
                plain(frag["quote"]), task["type"], options, ref, task["text"]
            ):
                continue
            picked.append({k: frag[k] for k in ("id", "material_id", "page", "file", "quote")})
        return picked[:MAX_CITATIONS]

    def _store(self, task, run, mode, message, reply, suggestions, citations, reason) -> None:
        # What the child can read, as one text: the judge reads it in later requests of the
        # same session and run (Flow._verdict), never an older session's.
        visible = "\n".join([reply, *suggestions, *(c["quote"] for c in citations)])
        data = {
            "mode": mode,
            "run_id": run["id"],
            "question": message,
            "reply": reply,
            "suggestions": suggestions,
            "citations": citations,
            "visible": visible,
            "withheld": reason,
        }
        self._exam_check(task)
        with atomic(self.conn):
            state.add_event(self.conn, self.now, "chat", task["id"], data)
