"""SQLite metadata store: schema bootstrap, connection lifecycle, helpers.

Connections are short-lived. Heavy ops follow the open-read-close-work-open-
write-close pattern; the watcher opens, writes, closes per event. WAL mode
keeps readers and the writer from blocking each other; FTS5 mirrors the
text columns of `docs` for `dnd grep` and multi-value boolean queries.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dragndoc.config import get_settings
from dragndoc.log import get_logger


log = get_logger(__name__)


SCHEMA_VERSION = "3"


_DOCS_FULL_SQL = """
CREATE VIEW IF NOT EXISTS docs_full AS
SELECT d.*,
       o.decision   AS ocr_decision,
       o.done       AS ocr_done,
       o.engine     AS ocr_engine,
       o.engine_ver AS ocr_engine_ver,
       o.langs      AS ocr_langs
FROM docs d
LEFT JOIN ocr o ON o.doc_id = d.id
"""


_TRIAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS triage (
    doc_id      INTEGER PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    enqueued_at TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'digested'
)
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS docs (
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
    dup         TEXT NOT NULL DEFAULT 'unique'
                CHECK (dup IN ('unique', 'dup', 'keep')),
    summary     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    extra       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_docs_hash       ON docs(hash);
CREATE INDEX IF NOT EXISTS ix_docs_category   ON docs(category);
CREATE INDEX IF NOT EXISTS ix_docs_digested   ON docs(digested);
CREATE INDEX IF NOT EXISTS ix_docs_dup        ON docs(dup);

CREATE TABLE IF NOT EXISTS ocr (
    doc_id      INTEGER PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,
    done        TEXT,
    engine      TEXT,
    engine_ver  TEXT,
    langs       TEXT NOT NULL DEFAULT ''
);

""" + _DOCS_FULL_SQL + """;

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, summary, notes, tags, parties,
    content=docs,
    content_rowid=id,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, summary, notes, tags, parties)
    VALUES (new.id, new.title, new.summary, new.notes, new.tags, new.parties);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, summary, notes, tags, parties)
    VALUES('delete', old.id, old.title, old.summary, old.notes, old.tags, old.parties);
END;

CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, summary, notes, tags, parties)
    VALUES('delete', old.id, old.title, old.summary, old.notes, old.tags, old.parties);
    INSERT INTO docs_fts(rowid, title, summary, notes, tags, parties)
    VALUES (new.id, new.title, new.summary, new.notes, new.tags, new.parties);
END;

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_id ON events(id);

""" + _TRIAGE_TABLE_SQL + """;
CREATE INDEX IF NOT EXISTS ix_triage_enqueued ON triage(enqueued_at);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_bootstrap_lock = threading.Lock()
_bootstrapped: set[Path] = set()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _read_ver(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'ver'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return str(row[0]) if row and row[0] else None


def _migrate_1_to_3(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "docs", "dup"):
        conn.execute(
            "ALTER TABLE docs ADD COLUMN dup TEXT NOT NULL DEFAULT 'unique' "
            "CHECK (dup IN ('unique', 'dup', 'keep'))"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_docs_dup ON docs(dup)")
    if _table_exists(conn, "triage_queue") and not _table_exists(conn, "triage"):
        conn.execute("ALTER TABLE triage_queue RENAME TO triage")
    if not _table_exists(conn, "triage"):
        conn.execute(_TRIAGE_TABLE_SQL)
    conn.execute("DROP INDEX IF EXISTS ix_triage_enqueued")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_triage_enqueued ON triage(enqueued_at)")
    conn.execute("DROP VIEW IF EXISTS docs_full")
    conn.execute(_DOCS_FULL_SQL)


_MIGRATIONS = [
    ("1", "3", _migrate_1_to_3),
]


def db_path() -> Path:
    """Resolve the DB file location from settings."""
    settings = get_settings()
    return settings.db_path


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


def bootstrap_schema(path: Path | None = None) -> None:
    """Create tables + indices + triggers + FTS5 if missing. Idempotent."""
    target = path or db_path()
    with _bootstrap_lock:
        if target in _bootstrapped and target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        existed_before_open = target.exists()
        conn = sqlite3.connect(str(target))
        try:
            _apply_pragmas(conn)
            had_docs_before_bootstrap = _table_exists(conn, "docs") if existed_before_open else False
            current = _read_ver(conn)
            if current is None:
                if not had_docs_before_bootstrap:
                    conn.executescript(_SCHEMA_SQL)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('ver', ?)",
                        (SCHEMA_VERSION,),
                    )
                    conn.commit()
                    _bootstrapped.add(target)
                    log.debug("schema bootstrapped at %s", target)
                    return
                raise RuntimeError("Existing database is missing schema_meta.ver; migrate it explicitly")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for from_ver, to_ver, fn in _MIGRATIONS:
                if current == from_ver:
                    fn(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('ver', ?)",
                        (to_ver,),
                    )
                    current = to_ver
            if current != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema version: {current}")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('ver', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()
        _bootstrapped.add(target)
        log.debug("schema bootstrapped at %s", target)


def reset_bootstrap_cache() -> None:
    """Drop the bootstrap memo. Used by tests when DB paths change between runs."""
    with _bootstrap_lock:
        _bootstrapped.clear()


@contextmanager
def connect(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield a connection with PRAGMAs applied; close on exit.

    The DB is bootstrapped on first use. ``readonly=True`` opens with the
    URI form so concurrent writers aren't blocked by long-running reads.
    """
    target = db_path()
    bootstrap_schema(target)
    if readonly:
        uri = f"file:{target}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a single transaction. Commits on success."""
    with connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Semilist helpers — all multi-valued string columns share this format.
# Storage is ``';a;b;c;'`` for non-empty (sorted, deduped, ``;``-stripped from
# values) and ``''`` for empty. Membership: ``LIKE '%;X;%'``. Multi-value
# AND: combine multiple LIKE clauses with AND. Boolean ops: use FTS5.
# ---------------------------------------------------------------------------


def to_semilist(values: list[str] | tuple[str, ...] | None) -> str:
    if not values:
        return ""
    cleaned = sorted({v.replace(";", "") for v in values if v})
    return ";" + ";".join(cleaned) + ";" if cleaned else ""


def from_semilist(s: str | None) -> list[str]:
    if not s:
        return []
    return [v for v in s.strip(";").split(";") if v]


def semilist_contains(field_value: str, needle: str) -> bool:
    """Pure-Python equivalent of ``LIKE '%;needle;%'`` — used by tests."""
    if not field_value or not needle:
        return False
    return f";{needle.replace(';', '')};" in field_value
