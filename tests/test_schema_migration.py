"""Schema migration tests."""

from __future__ import annotations

import sqlite3

from dragndoc.db import bootstrap_schema, connect


_V1_DOCS_SQL = """
CREATE TABLE docs (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    hash        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    modified    TEXT,
    digested    TEXT,
    original    TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'Unknown',
    parties     TEXT NOT NULL DEFAULT '',
    langs       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    date        TEXT,
    title       TEXT,
    confidence  TEXT NOT NULL DEFAULT 'low'
                CHECK (confidence IN ('low', 'medium', 'high', 'confirmed')),
    summary     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    extra       TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE ocr (
    doc_id      INTEGER PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,
    done        TEXT,
    engine      TEXT,
    engine_ver  TEXT,
    langs       TEXT NOT NULL DEFAULT ''
);
CREATE VIEW docs_full AS
SELECT d.*,
       o.decision   AS ocr_decision,
       o.done       AS ocr_done,
       o.engine     AS ocr_engine,
       o.engine_ver AS ocr_engine_ver,
       o.langs      AS ocr_langs
FROM docs d
LEFT JOIN ocr o ON o.doc_id = d.id;
CREATE TABLE triage_queue (
    doc_id      INTEGER PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    enqueued_at TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'digested'
);
INSERT INTO docs (id, path, hash, size, original, category, summary)
VALUES (1, 'Inbox/a.txt', 'sha256:a', 1, 'a.txt', 'Personal', 'summary');
INSERT INTO triage_queue (doc_id, enqueued_at, reason)
VALUES (1, '2026-01-01T00:00:00Z', 'digested');
"""


def _seed_v1(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_DOCS_SQL)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES ('ver', '1');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_schema_migration_v1_to_v3(docs_root):
    from dragndoc.config import get_settings

    db_path = get_settings().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_v1(db_path)
    bootstrap_schema(db_path)
    with connect(readonly=True) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(docs)").fetchall()}
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'ver'").fetchone()
        row = conn.execute("SELECT dup FROM docs WHERE path = 'Inbox/a.txt'").fetchone()
        queued = conn.execute("SELECT reason FROM triage WHERE doc_id = 1").fetchone()
        view = conn.execute("SELECT dup FROM docs_full WHERE path = 'Inbox/a.txt'").fetchone()
    assert ver["value"] == "4"
    assert "dup" in columns
    assert "triage" in tables
    assert "dirs" in tables
    assert "triage_queue" not in tables
    assert row["dup"] == "unique"
    assert queued["reason"] == "digested"
    assert view["dup"] == "unique"


def test_schema_migration_fresh_db(docs_root):
    from dragndoc.config import get_settings

    db_path = get_settings().db_path
    bootstrap_schema(db_path)
    with connect(readonly=True) as conn:
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'ver'").fetchone()
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert ver["value"] == "4"
    assert "docs" in tables
    assert "dirs" in tables
    assert "triage" in tables
    assert "triage_queue" not in tables
