-- Revision-bound model evidence and a shared, restart-safe publication sample.
CREATE TABLE figure_assessment (
    task_item_id INTEGER NOT NULL REFERENCES task_item(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    report TEXT NOT NULL CHECK(json_valid(report)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_item_id, revision)
);
CREATE TABLE pipeline_sampling_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    passed INTEGER NOT NULL DEFAULT 0,
    clean_streak INTEGER NOT NULL DEFAULT 0
);
INSERT INTO pipeline_sampling_state(id) VALUES(1);
CREATE TABLE pipeline_sample (
    task_item_id INTEGER NOT NULL REFERENCES task_item(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    chosen INTEGER NOT NULL CHECK(chosen IN (0,1)),
    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('clean','edited','excluded')),
    PRIMARY KEY(task_item_id, revision)
);
CREATE TABLE generated_figure_package (
    material_id INTEGER PRIMARY KEY REFERENCES material(id) ON DELETE CASCADE,
    payload_sha256 TEXT NOT NULL
);
