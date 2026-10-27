"""Шаги с моделью в фоне (OH-T06b).

POST проверяет запрос, берёт замок `Inflight`, записывает шаг (`tutor_step`) и отдаёт работу
рабочему потоку со своим соединением с базой. Экран опрашивает сервер; «идёт ли шаг» и «сколько
он идёт» берётся из замка и строки шага, а не из JS: страница после перезагрузки или другое
устройство видит тот же шаг.

Срок шага: один шаг - до 4 обращений к модели (текст, судья, перегенерация, судья), у каждого
таймаут исполнителя 130 с; берём вдвое больше этого запаса. Шаг, который идёт дольше или потерял
замок (перезапуск процесса), помечается failed при следующем чтении, замок снимается.
"""

import logging
import threading
from datetime import datetime

from app.ingest.store import open_db
from app.model.client import REQUEST_TIMEOUT_S

log = logging.getLogger(__name__)

STEP_CALLS = 4
STEP_TIMEOUT_S = 2 * REQUEST_TIMEOUT_S * STEP_CALLS
FAILED_TEXT = "Помощник не ответил вовремя"


class StepExpired(Exception):
    """Шаг снят по сроку или потерял замок: его результат нельзя публиковать."""


def start(conn, task_id: int, kind: str, now: datetime) -> int:
    """Строка шага в состоянии pending: идущим (running) он становится, когда взял замок."""
    return conn.execute(
        "INSERT INTO tutor_step (task_item_id, kind, started_at) VALUES (?, ?, ?)",
        (task_id, kind, now.isoformat()),
    ).lastrowid


def activate(conn, step_id: int) -> None:
    conn.execute("UPDATE tutor_step SET status = 'running' WHERE id = ?", (step_id,))


def discard(conn, step_id: int) -> None:
    conn.execute("DELETE FROM tutor_step WHERE id = ? AND status = 'pending'", (step_id,))


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


def finish(
    conn, step_id: int, status: str, now: datetime, *, panel_state=None, notice=None, error=None
):
    """Итог пишет только шаг, который ещё идёт: просроченный шаг не перетирается позднее."""
    conn.execute(
        "UPDATE tutor_step SET status = ?, finished_at = ?, panel_state = ?, notice = ?, error = ? "
        "WHERE id = ? AND status = 'running'",
        (status, now.isoformat(), panel_state, notice, error, step_id),
    )


def get(conn, step_id: int):
    return conn.execute("SELECT * FROM tutor_step WHERE id = ?", (step_id,)).fetchone()


def latest(conn, task_id: int):
    return conn.execute(
        "SELECT * FROM tutor_step WHERE task_item_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


RANK = {"pending": 1, "running": 2, "done": 3, "failed": 3}


def version(conn, task_id: int) -> str:
    """Версия экрана «<прогон>.<событие>.<шаг>.<ранг>», сравнивается по частям слева направо и растёт
    монотонно при ЛЮБОМ изменении состояния панели: новый прогон (повтор), попытка, подсказка,
    отметка ключа, объяснение (последний id события задания), новый шаг, переход шага
    (идёт < окончен). Экран без прогонов, событий и шагов - «0.0.0.0». Клиент применяет ответ,
    только если версия не меньше уже применённой, поэтому опоздавший ответ (POST или опрос) не
    откатывает экран. Строка «pending» живёт внутри одного запроса и может исчезнуть (discard),
    поэтому в версию не входит."""
    run = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM tutor_run WHERE task_item_id = ?", (task_id,)
    ).fetchone()[0]
    seq = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM event WHERE task_item_id = ?", (task_id,)
    ).fetchone()[0]
    row = conn.execute(
        "SELECT id, status FROM tutor_step WHERE task_item_id = ? AND status != 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    step, rank = (0, 0) if row is None else (row["id"], RANK.get(row["status"], 3))
    return f"{run}.{seq}.{step}.{rank}"


def elapsed(row, now: datetime) -> int:
    return max(0, int((now - datetime.fromisoformat(row["started_at"])).total_seconds()))


def expire(conn, inflight, task_id: int, now: datetime) -> None:
    """Зависший или потерявший замок шаг -> failed, замок снят (только если он этого шага)."""
    row = conn.execute(
        "SELECT * FROM tutor_step WHERE task_item_id = ? AND status = 'running' ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for step in row:
        holder = inflight.step_of(task_id)
        lost = inflight.since(task_id) is None or (holder is not None and holder != step["id"])
        late = elapsed(step, now) > STEP_TIMEOUT_S
        if not (lost or late):
            continue
        reason = "timeout" if late else "interrupted"
        finish(conn, step["id"], "failed", now, error=reason)
        inflight.release(task_id, step["id"])


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

    def _run(self, cls, task_id, step_id, name, args) -> None:
        st = self.state
        conn = None
        try:
            conn = open_db(st.ctx.db_path)
            flow = cls(conn, st.model, st.inflight, st.clock)
            flow.step_id = step_id
            flow.execute(task_id, name, args, reraise=False)
        except BaseException:
            log.exception("шаг %s не стартовал", step_id)
            st.inflight.release(task_id, step_id)
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
