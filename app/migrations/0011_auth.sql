-- OH-T08: сессии родителя, защита входа по PIN, уведомление ребёнку, удаление материалов.

-- Сессия родителя живёт на сервере. Cookie хранит случайный id; в базе - только его sha256,
-- поэтому копия базы не даёт войти. csrf_token привязан к сессии.
CREATE TABLE parent_session (
    id_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Одна строка: счётчик неверных PIN и время, до которого вход закрыт. Не зависит от cookie.
CREATE TABLE login_guard (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    locked_until TEXT
);
INSERT INTO login_guard (id) VALUES (1);

-- Когда ребёнок подтвердил уведомление о внешней модели (SPEC 1.6).
ALTER TABLE settings ADD COLUMN child_notice_seen_at TEXT;

-- Материал, у которого удалили файл и страницы, но остались подтверждённые задания.
ALTER TABLE material ADD COLUMN deleted_at TEXT;
