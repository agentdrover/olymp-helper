"""Что видит ребёнок на экране задания. Единственное место, где состояние разбора превращается
в ответ: страница, HTMX-фрагмент и JSON строятся из одного словаря. Ключ, model_answer и
teacher_note сюда не попадают; ответ ключа есть только в `solution` и только при открытом решении.
"""

import json
from datetime import datetime

from app.chat import store as chat_store
from app.ingest.publish import assist_mode, child_view, has_key
from app.tutor import seq, state, steps
from app.tutor.panel import BUSY, Inflight, live_state, panel


def _solution(run) -> dict | None:
    if run is None or not state.is_terminal(run) or not run["solution_json"]:
        return None
    sol = json.loads(run["solution_json"])
    note = None
    if sol.get("unverified"):
        note = state.UNVERIFIED
    if sol.get("disputed"):
        note = state.DISPUTED
    return {
        "answer": sol["answer"],
        "explanation": None if sol.get("disputed") else sol.get("explanation_html"),
        "note": note,
        "unverified": bool(sol.get("unverified")),
        "disputed": bool(sol.get("disputed")),
    }


def snapshot_live(conn, inflight: Inflight, task_id: int, now: datetime) -> str:
    """Идёт ли шаг. Читать ПЕРВЫМ, до прогона и итога шага: закончившийся между чтениями шаг
    тогда виден как «думаю» (опрос продолжится), а не как «пусто» при устаревшем прогоне."""
    steps.expire(conn, inflight, task_id, now)  # зависший шаг не держит панель вечно
    return live_state(conn, inflight, task_id)


def build_view(
    conn, task, material, run, now: datetime, *, inflight: Inflight, panel_state=None, notice=None,
    live=None,
) -> dict:  # fmt: skip
    keyed = has_key(task)
    assist = assist_mode(task)
    terminal = run is not None and state.is_terminal(run)
    current = run["state"] if run else "new"
    attempts = run["attempts"] if run else 0
    hints = run["hints"] if run else 0
    allowed = state.checks_allowed(task, run) if run else state.BASE_CHECKS
    nxt = None if terminal or assist != "hints" or hints >= 3 else hints + 1
    wait = 0
    if nxt == 1:
        wait = state.hint_wait(run, now) if run else state.HINT_WAIT_S
    last = None
    if run and not terminal and run["last_answer"]:
        last = {"answer": run["last_answer"], "mark": "не то" if keyed else "не совпало"}
    analysis = None
    if run and not terminal and run["analysis_json"]:
        analysis = json.loads(run["analysis_json"])["html"]
    if live is None:
        live = snapshot_live(conn, inflight, task["id"], now)
    shown = panel_state if panel_state and panel_state != "idle" else live
    # шаг мог закончиться после снимка «идёт»: его номер всё равно отдаём, чтобы опрос забрал итог
    running = None
    if shown in BUSY:
        running = steps.get(conn, inflight.step_of(task["id"])) or steps.latest(conn, task["id"])
    if running is None and shown not in BUSY and not notice:
        last_step = steps.latest(conn, task["id"])
        if shown == "idle" and last_step and last_step["status"] == "failed":
            shown = "step_failed"  # шаг не закончился (сбой, срок, перезапуск): не «думаю» вечно
    extra = {}
    if running is not None:
        extra = {
            "step": running["id"],
            "started_at": running["started_at"],
            "elapsed_s": steps.elapsed(running, now),
        }
    correction = None
    if not terminal and attempts == 0:
        stale = conn.execute(
            "SELECT 1 FROM tutor_run WHERE task_item_id = ? AND closed_at IS NOT NULL "
            "AND (revision <> ? OR key_version <> ?) LIMIT 1",
            (task["id"], task["revision"], task["key_version"]),
        ).fetchone()
        if stale:
            correction = (
                "Задание или ключ к нему исправили после твоей прошлой попытки: разбираем заново."
            )
    chat = chat_store.screen_items(
        conn, chat_store.open_session(conn, now), task["id"], run["id"] if run else None
    )
    return {
        "ok": True,
        "correction": correction,
        "chat": chat,
        "compose": chat_store.compose_of(chat),
        "state": current,
        "terminal": terminal,
        "repeat": bool(run["is_repeat"]) if run else False,
        "attempts": attempts,
        "hints": hints,
        "checks_left": 0 if terminal else max(0, allowed - attempts),
        "assist": assist,
        "has_key": keyed,
        "last": last,
        "hints_list": [
            {"level": h["level"], "html": h["html"]}
            for h in (json.loads(run["hints_json"]) if run else [])
        ],
        "analysis": analysis,
        "solution": _solution(run),
        "outcome": run["outcome"] if run and terminal else None,
        "explains": json.loads(run["explains_json"]) if run and terminal else [],
        "hint": {
            "next": nxt,
            "wait_s": wait,
            "ready": nxt is not None and wait == 0,
            "needs_confirm": nxt == 3,
        },
        "can_show_answer": assist == "key_only" and not terminal and attempts >= state.BASE_CHECKS,
        "panel": {**panel(shown, **extra), "version": seq.current(conn, task["id"])},
        "notice": notice,
        "task": child_view(task, material),
    }
