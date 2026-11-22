"""Шаги с моделью в фоне (OH-T06b).

POST проверяет запрос, берёт замок `Inflight`, записывает шаг (`tutor_step`) и отдаёт работу
рабочему потоку со своим соединением с базой. Экран опрашивает сервер; «идёт ли шаг» и «сколько
он идёт» берётся из замка и строки шага, а не из JS: страница после перезагрузки или другое
устройство видит тот же шаг.

Срок шага: один шаг - до 4 обращений к модели (текст, судья, перегенерация, судья), у каждого
таймаут исполнителя 130 с; берём вдвое больше этого запаса. Шаг, который идёт дольше или потерял
замок (перезапуск процесса), помечается failed при следующем чтении, замок снимается.
"""

import json
import logging
import threading
from contextlib import closing
from datetime import datetime

from app import exam
from app.ingest.publish import is_published
from app.ingest.store import atomic, get_task, open_db
from app.model.client import REQUEST_TIMEOUT_S
from app.model.types import BoundaryWait
from app.tutor.seq import bump

log = logging.getLogger(__name__)

STEP_CALLS = 4
STEP_TIMEOUT_S = 2 * REQUEST_TIMEOUT_S * STEP_CALLS
FAILED_TEXT = "Помощник не ответил вовремя"


class StepExpired(Exception):
    """Шаг снят по сроку или потерял замок: его результат нельзя публиковать."""


def start(conn, task_id: int, kind: str, now: datetime) -> int:
    """Строка шага в состоянии pending: идущим (running) он становится, когда взял замок."""
    with atomic(conn):  # запись и версия панели - одна транзакция
        step_id = conn.execute(
            "INSERT INTO tutor_step (task_item_id, kind, started_at) VALUES (?, ?, ?)",
            (task_id, kind, now.isoformat()),
        ).lastrowid
        bump(conn)
        return step_id


def activate(conn, step_id: int) -> None:
    with atomic(conn):
        conn.execute("UPDATE tutor_step SET status = 'running' WHERE id = ?", (step_id,))
        bump(conn)


def discard(conn, step_id: int) -> None:
    with atomic(conn):
        gone = conn.execute(
            "DELETE FROM tutor_step WHERE id = ? AND status = 'pending'", (step_id,)
        )
        if gone.rowcount:
            bump(conn)


def hold(conn, step_id: int | None) -> None:
    """Сравнить-и-держать: вызывается ВНУТРИ транзакции записи результата. Шаг ещё идёт - строка
    шага залочена этой же транзакцией, снять шаг по сроку одновременно нельзя; не идёт -
    StepExpired, транзакция откатывается и ничего не публикуется. Все писатели результата
    (подсказка, разбор ошибки, объяснение, чат, расхождение) проходят через эту функцию."""
    if step_id is None:
        return
    done = conn.execute(
        "UPDATE tutor_step SET finished_at = finished_at WHERE id = ? AND status = 'running'",
        (step_id,),
    )
    if done.rowcount != 1:
        raise StepExpired(f"шаг {step_id} уже снят")
    conn.execute("UPDATE tutor_pending_work SET applied = 1 WHERE step_id = ?", (step_id,))


def finish(
    conn, step_id: int, status: str, now: datetime, *, panel_state=None, notice=None, error=None
):
    """Итог пишет только шаг, который ещё идёт: просроченный шаг не перетирается позднее."""
    with atomic(conn):
        done = conn.execute(
            "UPDATE tutor_step SET status = ?, finished_at = ?, panel_state = ?, notice = ?, "
            "error = ? WHERE id = ? AND status = 'running'",
            (status, now.isoformat(), panel_state, notice, error, step_id),
        )
        if done.rowcount:
            bump(conn)


def get(conn, step_id: int):
    return conn.execute("SELECT * FROM tutor_step WHERE id = ?", (step_id,)).fetchone()


def latest(conn, task_id: int):
    return conn.execute(
        "SELECT * FROM tutor_step WHERE task_item_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


def waiting(conn, task_id: int):
    return conn.execute(
        "SELECT s.*, m.status AS operation_status "
        "FROM tutor_step s JOIN tutor_pending_work w ON w.step_id = s.id "
        "JOIN model_operation m ON m.kind = 'tutor' AND m.object_key = CAST(s.id AS TEXT) "
        "WHERE s.task_item_id = ? AND s.status = 'running' AND m.status IN ('queued', 'active', 'unknown') "
        "ORDER BY s.id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def elapsed(row, now: datetime) -> int:
    return max(0, int((now - datetime.fromisoformat(row["started_at"])).total_seconds()))


def expire(conn, inflight, task_id: int, now: datetime) -> None:
    """Зависший или потерявший замок шаг -> failed, замок снят (только если он этого шага)."""
    row = conn.execute(
        "SELECT * FROM tutor_step WHERE task_item_id = ? AND status = 'running' ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for step in row:
        queued = waiting(conn, task_id)
        if queued is not None and queued["id"] == step["id"]:
            continue
        holder = inflight.step_of(task_id)
        lost = inflight.since(task_id) is None or (holder is not None and holder != step["id"])
        late = elapsed(step, now) > STEP_TIMEOUT_S
        if not (lost or late):
            continue
        reason = "timeout" if late else "interrupted"
        finish(conn, step["id"], "failed", now, error=reason)
        inflight.release(task_id, step["id"], conn)


class StepRunner:
    """Запускает работу шага. sync=True (тесты) выполняет её в потоке запроса."""

    def __init__(self, state, *, sync: bool = False):
        self.state = state  # app.state: model, inflight, clock, ctx читаются при запуске
        self.sync = sync
        self.started = 0
        self._threads: list[threading.Thread] = []

    def submit(self, cls, task_id: int, step_id: int, name: str, args: tuple) -> None:
        self.started += 1
        worker = threading.Thread(
            target=self._run, args=(cls, task_id, step_id, name, args), daemon=True,
            name=f"step-{step_id}",
        )  # fmt: skip
        self._threads.append(worker)
        worker.start()

    def enqueue(self, conn, task, step_id, name, args, now) -> None:
        session_id = None
        with atomic(conn):
            if name == "_chat_work":
                from app.tutor.state import session_id_for

                session_id = session_id_for(conn, now)
            conn.execute(
                "INSERT INTO tutor_pending_work "
                "(step_id, method, arguments, session_id, revision, key_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    step_id,
                    name,
                    json.dumps(args),
                    session_id,
                    task["revision"],
                    task["key_version"],
                    now.isoformat(),
                ),
            )
            self.state.model_work.submit("tutor", step_id, conn=conn)
            self.state.inflight.release(task["id"], step_id)
            bump(conn)

    def run_queued(self, key: str) -> None:
        from app.chat.flow import ChatFlow
        from app.tutor.flow import Flow

        step_id = int(key)
        with closing(open_db(self.state.ctx.db_path)) as conn, atomic(conn):
            row = conn.execute(
                "SELECT s.task_item_id, w.method, w.arguments, w.revision, w.key_version, w.applied "
                "FROM tutor_pending_work w JOIN tutor_step s ON s.id = w.step_id "
                "WHERE w.step_id = ? AND s.status = 'running'",
                (step_id,),
            ).fetchone()
            if row is None:
                return
            method = row["method"]
            if method not in (
                "_chat_work",
                "_attempt_work",
                "_solution_work",
                "_hint_work",
                "_explain_work",
            ):
                raise ValueError("unknown tutor operation")
            task_id = row["task_item_id"]
            now = self.state.clock()
            if row["applied"]:
                finish(conn, step_id, "done", now)
                conn.execute("DELETE FROM tutor_pending_work WHERE step_id = ?", (step_id,))
                return
            task = get_task(conn, task_id)
            if (
                task is None
                or not is_published(task)
                or task["revision"] != row["revision"]
                or task["key_version"] != row["key_version"]
                or exam.task_locked(conn, task_id, now)
            ):
                conn.execute("DELETE FROM tutor_pending_work WHERE step_id = ?", (step_id,))
                finish(conn, step_id, "failed", now, error="superseded")
                return
            last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
            inflight = self.state.inflight
            if inflight.step_of(task_id) != step_id and not inflight.acquire(
                task_id,
                last,
                step_id,
                conn,
            ):
                raise RuntimeError("another step is running")
            conn.execute(
                "UPDATE tutor_step SET started_at = ? WHERE id = ?", (now.isoformat(), step_id)
            )
            bump(conn)
        executed = self._run(
            ChatFlow if method == "_chat_work" else Flow,
            task_id,
            step_id,
            method,
            tuple(json.loads(row["arguments"])),
        )
        if executed:
            with closing(open_db(self.state.ctx.db_path)) as conn:
                conn.execute("DELETE FROM tutor_pending_work WHERE step_id = ?", (step_id,))

    def _run(self, cls, task_id, step_id, name, args) -> bool:
        st = self.state
        conn = None
        try:
            conn = open_db(st.ctx.db_path)
            flow = cls(conn, st.model, st.inflight, st.clock)
            flow.step_id = step_id
            flow.execute(task_id, name, args, reraise=False)
            return True
        except BoundaryWait:
            st.inflight.release(task_id, step_id, conn)
            return False
        except BaseException:
            log.exception("шаг %s не стартовал", step_id)
            st.inflight.release(task_id, step_id, conn)
            return False
        finally:
            if conn is not None:
                conn.close()

    def join_step(self, step_id: int, timeout: float = 20) -> None:
        for worker in list(self._threads):
            if worker.name == f"step-{step_id}":
                worker.join(timeout)

    def join(self, timeout: float = 20) -> None:
        """Подождать рабочие потоки (тесты)."""
        for worker in list(self._threads):
            worker.join(timeout)
        self._threads = [w for w in self._threads if w.is_alive()]
