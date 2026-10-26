"""Вход родителя по PIN: хэш PIN, защита от перебора, серверные сессии (OH-T08).

Все решения принимает сервер и хранит их в базе: счётчик неверных PIN и время блокировки не
зависят от cookie, сессия живёт 30 минут простоя и стирается с сервера.
"""

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

SESSION_IDLE = timedelta(minutes=30)
MAX_FAILURES = 5
LOCK_FOR = timedelta(minutes=5)
PIN_LENGTH = 4

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def valid_pin(pin: str) -> bool:
    return len(pin) == PIN_LENGTH and pin.isascii() and pin.isdigit()


def hash_pin(pin: str) -> str:
    """scrypt с солью. Формат: scrypt$N$r$p$соль$хэш (base64)."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    parts = ["scrypt", str(_SCRYPT["n"]), str(_SCRYPT["r"]), str(_SCRYPT["p"])]
    parts += [base64.b64encode(salt).decode(), base64.b64encode(digest).decode()]
    return "$".join(parts)


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            pin.encode("utf-8"),
            salt=base64.b64decode(salt),
            dklen=32,
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(digest, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def local_time(moment: datetime) -> str:
    """ЧЧ:ММ для экрана входа: часовой пояс из OLYMP_TZ (имя IANA), иначе пояс сервера."""
    name = os.environ.get("OLYMP_TZ")
    if name:
        try:
            return moment.astimezone(ZoneInfo(name)).strftime("%H:%M")
        except (KeyError, ValueError, OSError):
            pass
    return moment.astimezone().strftime("%H:%M")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- блокировка


@dataclass(frozen=True)
class Verdict:
    kind: str  # ok | wrong | locked | not_set
    left: int = 0  # сколько попыток осталось (для wrong)
    until: datetime | None = None  # до какого времени закрыт вход (для locked)


def locked_until(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    row = conn.execute("SELECT locked_until FROM login_guard WHERE id = 1").fetchone()
    if row is None or row[0] is None:
        return None
    until = datetime.fromisoformat(row[0])
    return until if until > now else None


def failures_left(conn: sqlite3.Connection) -> int:
    return MAX_FAILURES - conn.execute("SELECT failures FROM login_guard").fetchone()[0]


def register_failure(conn: sqlite3.Connection, now: datetime) -> Verdict:
    """Одна неверная попытка; пятая закрывает вход на 5 минут. Вызывать внутри транзакции."""
    failures = conn.execute("SELECT failures FROM login_guard WHERE id = 1").fetchone()[0] + 1
    if failures >= MAX_FAILURES:
        until = now + LOCK_FOR
        conn.execute(
            "UPDATE login_guard SET failures = 0, locked_until = ? WHERE id = 1", (_iso(until),)
        )
        return Verdict("locked", until=until)
    conn.execute("UPDATE login_guard SET failures = ? WHERE id = 1", (failures,))
    return Verdict("wrong", left=MAX_FAILURES - failures)


def reset_guard(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE login_guard SET failures = 0, locked_until = NULL WHERE id = 1")


def attempt_pin(conn: sqlite3.Connection, now: datetime, pin: str) -> Verdict:
    """Проверка PIN. Закрытый вход не проверяет PIN вообще: верный PIN тоже не впускает."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        until = locked_until(conn, now)
        if until is not None:
            verdict = Verdict("locked", until=until)
        else:
            stored = conn.execute("SELECT parent_pin_hash FROM settings WHERE id = 1").fetchone()[0]
            if stored is None:
                verdict = Verdict("not_set")
            elif valid_pin(pin) and verify_pin(pin, stored):
                reset_guard(conn)
                verdict = Verdict("ok")
            else:
                verdict = register_failure(conn, now)
        conn.execute("COMMIT")
        return verdict
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def pin_is_set(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT parent_pin_hash FROM settings WHERE id = 1").fetchone()
    return row is not None and row[0] is not None


def setup_pin(conn: sqlite3.Connection, now: datetime, key_ok: bool, pin: str) -> Verdict:
    """Первичная установка PIN. Неверный ключ считается так же, как неверный PIN.
    Условие `parent_pin_hash IS NULL` в самом UPDATE делает установку одноразовой."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        until = locked_until(conn, now)
        if until is not None:
            verdict = Verdict("locked", until=until)
        elif pin_is_set(conn):
            verdict = Verdict("not_set")  # PIN уже есть: установка закрыта
        elif not key_ok:
            verdict = register_failure(conn, now)
        else:
            cur = conn.execute(
                "UPDATE settings SET parent_pin_hash = ? WHERE id = 1 AND parent_pin_hash IS NULL",
                (hash_pin(pin),),
            )
            if cur.rowcount == 1:
                reset_guard(conn)
            verdict = Verdict("ok" if cur.rowcount == 1 else "not_set")
        conn.execute("COMMIT")
        return verdict
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ----------------------------------------------------------------------------- сессии


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def create_session(conn: sqlite3.Connection, now: datetime) -> tuple[str, str]:
    """Новая сессия: (id для cookie, csrf-токен). В базе только sha256 id."""
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute("DELETE FROM parent_session WHERE last_seen_at <= ?", (_iso(now - SESSION_IDLE),))
    conn.execute(
        "INSERT INTO parent_session (id_hash, csrf_token, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (_sid_hash(sid), csrf, _iso(now), _iso(now)),
    )
    return sid, csrf


def lookup_session(conn: sqlite3.Connection, now: datetime, sid: str) -> str | None:
    """csrf-токен живой сессии или None. Простой не меньше 30 минут стирает сессию;
    иначе время последнего действия обновляется."""
    if not sid:
        return None
    key = _sid_hash(sid)
    row = conn.execute(
        "SELECT csrf_token, last_seen_at FROM parent_session WHERE id_hash = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    if datetime.fromisoformat(row[1]) <= now - SESSION_IDLE:
        conn.execute("DELETE FROM parent_session WHERE id_hash = ?", (key,))
        return None
    conn.execute("UPDATE parent_session SET last_seen_at = ? WHERE id_hash = ?", (_iso(now), key))
    return row[0]


def end_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM parent_session WHERE id_hash = ?", (_sid_hash(sid),))


def purge_sessions(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute(
        "DELETE FROM parent_session WHERE last_seen_at <= ?", (_iso(now - SESSION_IDLE),)
    )
    return cur.rowcount
