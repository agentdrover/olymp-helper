CREATE TABLE model_admission (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    accepting INTEGER NOT NULL CHECK (accepting IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 0)
);
INSERT INTO model_admission VALUES (1, 1, 0);

-- Queue metadata names domain records. Model inputs and outputs never go here.
CREATE TABLE model_operation (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    object_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'active', 'unknown', 'done')),
    UNIQUE (kind, object_key)
);
CREATE INDEX model_operation_status ON model_operation(status, id);

-- Domain continuation for a tutor step. A pending chat message is subject to
-- the same deletion and retention policy as the ordinary conversation.
CREATE TABLE tutor_pending_work (
    step_id INTEGER PRIMARY KEY REFERENCES tutor_step(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    arguments TEXT NOT NULL,
    session_id INTEGER REFERENCES study_session(id),
    revision INTEGER NOT NULL,
    key_version INTEGER NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE model_dispatch (
    operation_id INTEGER NOT NULL REFERENCES model_operation(id),
    ordinal INTEGER NOT NULL,
    attempt TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    run_id INTEGER NOT NULL UNIQUE REFERENCES model_run(id),
    disposition TEXT NOT NULL DEFAULT 'reserved'
        CHECK (disposition IN ('reserved', 'confirmed', 'unknown')),
    PRIMARY KEY(operation_id, ordinal)
);
