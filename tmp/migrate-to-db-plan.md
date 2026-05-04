# Migrate from sidecars to a SQLite metadata DB

Plan only — no code changes yet. Iterate on this doc until the shape is right, then implement in phases.

## Decisions made

1. **Storage location.** Rename `storage/` → `data/`. DB lives at `data/dragndoc.db`. Local disk only — never on OneDrive. Backed up via the existing periodic backup of the `data/` folder.
2. **Schema shape.** Most fields are real columns. Operational OCR state is its own table. Catch-all `extra` JSON column for ad-hoc fields. Multi-valued strings (`parties`, `langs`, `tags`) are semicolon-wrapped TEXT, not JSON arrays — uniform format, LLM-friendly, grep-friendly.
3. **Indexed keys.** `path` (unique), `hash`, `category`, `digested` — covers reconcile, hash lookup, browse, and "what needs work."
4. **Connection lifecycle.** No long-held DB lock. Heavy operations (`scan`, `reconcile`) build their working set in memory from a single read pass, do all work in memory, then write back in one batched transaction. Watcher opens-writes-closes per event batch.
5. **Events.** `data/events.jsonl` goes away. Replaced by an `events` table; toaster polls `SELECT ... WHERE id > cursor`.
6. **Migration path.** No importer, no fallback corpus. Day one of the migration deletes every existing `.meta/` folder and runs the pipeline from a blank DB.
7. **Triage skill** uses `dnd meta get` instead of reading sidecars directly.
8. **`dnd grep`** new command for searching metadata text.
9. **Editing UX.** Three flavors: structured per-field, file-apply, editor-roundtrip. On-disk edit format is markdown + YAML frontmatter (LLM-friendly, no JSON-escaping pain).

## Naming discipline

- Singular nouns where possible (`doc`, not `document`); plural where the field is genuinely multi-valued (`parties`, `langs`, `tags`).
- Common abbreviations applied: `version` → `ver`. Kept whole: `engine`, `decision` (load-bearing terms).
- Multi-valued string format: `';a;b;c;'` — leading + trailing `;`, empty string for none. **Sorted on write** for stable display, equality, and dedup. Forbid `;` in values (strip on write).

## Storage layout

```text
data/
├── dragndoc.db              # SQLite, WAL-mode, locally-backed-up
├── tessdata/                # unchanged
├── runtime/                 # unchanged (watch.pid, watch.disabled)
├── logs/                    # unchanged
└── toaster.cursor           # holds last-seen events.id (small int)
```

Gone: `data/scan/scan-*.json`, `data/events.jsonl`.

## Schema

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE docs (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,            -- relative to documents_root, /-separated
    hash        TEXT NOT NULL,                   -- sha256 of file bytes
    size        INTEGER NOT NULL,
    modified    TEXT,                            -- file's mtime at time of last digest (ISO 8601 UTC)
    digested    TEXT,                            -- when pipeline last ran successfully (ISO 8601 UTC)
    original    TEXT NOT NULL,                   -- original filename at creation, before any rename
    category    TEXT NOT NULL DEFAULT 'Unknown', -- slash-separated, arbitrary depth
    parties     TEXT NOT NULL DEFAULT '',        -- ;-wrapped, e.g. ';ACME;Tax Authority;'
    langs       TEXT NOT NULL DEFAULT '',        -- ;-wrapped, e.g. ';heb;eng;'
    tags        TEXT NOT NULL DEFAULT '',        -- ;-wrapped, e.g. ';tax-2025;receipt;'
    date        TEXT,                            -- document date, ISO 8601 or null
    title       TEXT,
    confidence  TEXT NOT NULL DEFAULT 'low'
                CHECK (confidence IN ('low', 'medium', 'high', 'confirmed')),
    summary     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    extra       TEXT NOT NULL DEFAULT '{}'       -- JSON dict; ad-hoc only
);

CREATE INDEX ix_docs_hash     ON docs(hash);
CREATE INDEX ix_docs_category ON docs(category);
CREATE INDEX ix_docs_digested ON docs(digested);

CREATE TABLE ocr (
    doc_id      INTEGER PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,                   -- 'ocr_full' | 'ocr_pages' | 'skip_encrypted' | ...
    done        TEXT,                            -- ISO 8601 UTC; null = analyzed but not OCR'd
    engine      TEXT,                            -- 'tesseract' (room for future engines)
    engine_ver  TEXT,
    langs       TEXT NOT NULL DEFAULT ''         -- ;-wrapped
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

CREATE TABLE events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'           -- JSON
);
CREATE INDEX ix_events_id ON events(id);

CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta(key, value) VALUES ('ver', '1');

-- Full-text search across the user-facing text fields.
-- 'unicode61 remove_diacritics 2' is the recommended general-purpose
-- tokenizer; splits on whitespace + punctuation (so ';' delimiters
-- naturally separate semilist values into individual tokens).
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title, summary, notes, tags, parties,
    content=docs,
    content_rowid=id,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, summary, notes, tags, parties)
    VALUES (new.id, new.title, new.summary, new.notes, new.tags, new.parties);
END;

CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, summary, notes, tags, parties)
    VALUES('delete', old.id, old.title, old.summary, old.notes, old.tags, old.parties);
END;

CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, summary, notes, tags, parties)
    VALUES('delete', old.id, old.title, old.summary, old.notes, old.tags, old.parties);
    INSERT INTO docs_fts(rowid, title, summary, notes, tags, parties)
    VALUES (new.id, new.title, new.summary, new.notes, new.tags, new.parties);
END;
```

### Why these shapes

- `docs` has 16 columns plus `extra`. `summary`, `notes`, `title`, `tags`, `parties` are mirrored into `docs_fts` for full-text and boolean queries.
- Absence of an `ocr` row means OCR was never analyzed for that doc — cleaner than a `decision='never'` sentinel.
- `docs_full` view keeps reads simple — most callers never care about the `docs`/`ocr` split. Writes still go to the underlying tables.
- `confidence ∈ {low, medium, high, confirmed}` absorbs the old `review` flag: `confirmed` = a human signed off. CHECK constraint enforces validity without the overhead of a lookup table.
- `events.id AUTOINCREMENT` is intentional — toaster's cursor relies on monotonic ids; reusing deleted ids would cause skipped events.
- `docs_fts` triggers fire on every insert/update/delete to `docs`. The maintenance cost is automatic; the ergonomic win is `MATCH 'tag1 AND tag2'`-style boolean queries on semilist fields.

### What `extra` absorbs (today)

- `amount` / `currency` — invoice-specific, low query volume.
- `filed_path` — derivable from `path` once filed.
- Anything LLMs invent that isn't yet promoted to a column.

### What's gone vs the old `MetadataDoc`

- `schema_version` per row — `ALTER TABLE ADD COLUMN` with defaults handles forward migration; per-row is YAGNI.
- `metadata_modified` / `metadata_modified_by` — audit not needed for a single-user tool.
- `filed_at` — `path NOT LIKE 'Inbox/%'` answers "is it filed?"; the timestamp is rarely queried.
- `review` — derived from `confidence = 'low' OR title IS NULL OR category = 'Unknown'`.
- `subcategory` — folded into slash-separated `category` (arbitrary depth).

## Connection / locking strategy

- `journal_mode=WAL`, `synchronous=NORMAL`. WAL handles concurrent readers + one writer cleanly without long lock holds.
- All access goes through a small `dragndoc/db.py` module exposing `connect()`, `transaction()`, and high-level helpers (`get_by_path`, `get_by_hash`, `upsert`, `mark_digested`, etc.). One connection per process per logical operation; closed at the end.
- Every connection sets `PRAGMA foreign_keys = ON;` (off by default in SQLite — easy to forget).
- Heavy ops (`scan`, `reconcile`, `dnd grep`):
  1. open connection (read-only or normal)
  2. `SELECT *` of the rows we care about into memory
  3. close connection
  4. walk the filesystem, build the result set in memory
  5. open connection, do all writes in one transaction, close
- Watcher: per-event handler opens a connection, does its writes, commits, closes. No connection held across idle periods.

## Multi-valued field handling

### Helpers

```python
# dragndoc/db.py
def to_semilist(values: list[str]) -> str:
    cleaned = sorted({v.replace(';', '') for v in values if v})
    return f";{';'.join(cleaned)};" if cleaned else ''

def from_semilist(s: str) -> list[str]:
    return [v for v in s.strip(';').split(';') if v] if s else []
```

`to_semilist` sorts and dedupes on write, so storage is canonical: equal sets produce equal strings, and equality queries / dedup work without per-call canonicalization.

### Querying

**Single-value membership** — direct LIKE on the column:

```sql
SELECT * FROM docs WHERE tags LIKE '%;tax-2025;%';
```

**Multi-value AND (intersection)** — multiple LIKE clauses joined by AND. Order in storage doesn't matter:

```sql
SELECT * FROM docs
WHERE tags LIKE '%;tax-2025;%' AND tags LIKE '%;business;%';
```

**Multi-value with operators (AND/OR/NOT/phrase)** — use FTS5:

```sql
SELECT d.* FROM docs d
JOIN docs_fts f ON d.id = f.rowid
WHERE docs_fts.tags MATCH 'tax-2025 AND (business OR vat) NOT draft';
```

FTS5's default tokenizer treats `;` as punctuation, so semilist fields tokenize cleanly into their constituent values without any extra work.

**Cross-column full-text search** — same FTS5 table without column scoping:

```sql
SELECT d.* FROM docs d
JOIN docs_fts f ON d.id = f.rowid
WHERE docs_fts MATCH 'invoice tax 2025'
ORDER BY bm25(docs_fts);
```

## Module-by-module changes

### New

- `dragndoc/db.py` — connection, schema bootstrap, helpers, semilist utils, in-process WAL checkpointing on idle.
- `dragndoc/meta_store.py` — high-level "metadata document" API used by pipeline / scanner / triage. Replaces `dragndoc/metadata/sidecar.py`.

### Heavily rewritten

- `dragndoc/metadata/schema.py` — `MetadataDoc` becomes a thin row mapper for `docs` + `ocr`. `to_row()` / `from_row()`. Drop YAML frontmatter formatters.
- `dragndoc/scanner.py` — no more on-disk worklists. `run_scan` returns a `Worklist` dataclass that lives only in memory; callers consume it directly. `_already_queued` and the `data/scan/scan-*.json` sweep go away.
- `dragndoc/metadata/reconcile.py` — orphan walk becomes "for each `docs` row, check the file exists at `path`; if not, look up by `hash` for moved candidates." Optional fast-path: only hash files matching the missing rows' sizes.
- `dragndoc/cli.py`:
  - `process` no longer takes a worklist path; runs scan + work in one shot. Optional `--rescan` flag is the natural default.
  - `scan` becomes a read-only diagnostic that prints what `process` would do (no file output).
  - `inspect` + `cat` collapse to `meta` (see below).
  - `mv`, `cp`, `rm` update the DB row instead of moving sidecars.
  - `ls` queries the DB for which paths have rows.
  - `reconcile` (or `review orphans`) becomes a DB-driven walk.
  - New: `dnd grep`, `dnd meta get/set/edit/apply/cat`.
- `dragndoc/events.py` → `append(kind, **fields)` writes to `events` table; `events_path` is removed.
- `dragndoc/toaster.py` — polls table instead of tailing the file. `data/toaster.cursor` now stores `last_id` integer.
- `dragndoc/pipeline.py` — writes to DB row instead of sidecar; same flow otherwise.
- `dragndoc/watcher.py` — open/close DB per event; same trigger logic.
- `dragndoc/bootstrap.py` — creates the DB if missing, runs schema bootstrap.
- `dragndoc/config.py` — rename `storage_dir` → `data_dir`; default `REPO_ROOT / "data"`. Drop `meta_subfolder`. Add `db_path` (default `data_dir / "dragndoc.db"`).

### Deleted

- `dragndoc/metadata/sidecar.py` — replaced by `meta_store.py`.
- All sidecar-quarantine logic (`is_quarantined`, the `*.broken-*` files, the `_quarantine` flow). DB rows can't be syntactically corrupt; schema-validation on read is enough.
- The `quarantined_sidecars` and `orphan_sidecars` shape in `Worklist`; replaced by a single `missing_files` bucket.

## CLI surface

```text
dnd
├── bootstrap
├── doctor
├── watch (start | stop | status | supervise)
├── process [path]                     # default: scan + process anything that needs it
├── scan                               # read-only diagnostic
├── review (ocr | orphans)
├── grep <pattern> [--field name]      # search summary/notes/title/parties/...
├── meta
│   ├── get <path>                     # JSON dump of one row (was `inspect`)
│   ├── cat <path>                     # markdown render (was `cat`)
│   ├── set <path> <field>=<value> ... # structured field edit
│   ├── apply <path> <file.md>         # whole-doc update from a markdown file
│   └── edit <path>                    # `git commit`-style: open temp .md in $EDITOR
├── ls
├── mv / cp / rm
└── toaster
```

## Editing UX (resolved)

On-disk edit format is **markdown + YAML frontmatter**. Same shape today's sidecars use; LLMs already handle it cleanly. Multi-valued fields render as YAML lists for editing, get converted to semilist strings on write:

```markdown
---
category: Finance/Receipts
parties: [ACME Corp]
tags: [tax-2025, business-expense]
langs: [eng]
date: 2025-09-12
title: ACME Q3 invoice
confidence: high
---
# Summary

Multi-paragraph text, quotes, whatever — no escaping needed.

# Notes

OCR'd content or hand notes here.
```

- `meta set <path> field=value ...` — structured, programmatic. JSON-typed values via `--json` for lists.
- `meta apply <path> <file.md>` — preferred path for LLM-driven edits.
- `meta edit <path>` — render → temp file → `$EDITOR` → parse + validate + write back on save.

JSON-format apply is supportable as `--format json` but is not the default.

## Implementation phases

Starting from scratch: day one of phase 1 nukes `.meta/` everywhere. There is no fallback corpus — the new pipeline rebuilds metadata from the files themselves.

1. **Foundation + scrub.** Add `db.py`, `meta_store.py`. Wire `bootstrap` to create the DB and run schema bootstrap (including `docs_fts` virtual table + triggers). Rename `storage_dir` → `data_dir`. Run `scripts/cleanup_sidecars.py` once to delete every `.meta/` folder. Delete `dragndoc/metadata/sidecar.py`. Remove `meta_subfolder` from config.
2. **Pipeline switch.** Rewire `pipeline.py`, `scanner.py`, `metadata/reconcile.py`, `watcher.py`, `mv`/`cp`/`rm`/`ls`/`inspect`/`cat` to read + write the DB. From now on, `dnd process` is the only way to populate metadata; running it from a clean DB rebuilds everything by re-extracting + re-enriching every file.
3. **Worklist removal.** Drop `data/scan/` JSON files. `process` does scan-in-memory by default. `scan` becomes diagnostic-only.
4. **Events table.** Migrate `events.jsonl` → `events` table. Toaster polls via SELECT. Delete the JSONL file.
5. **New CLI verbs.** `dnd grep` (FTS5-backed), `dnd meta {get,set,apply,edit,cat}`. Retire/rename old verbs (`inspect`, `cat`, `review-ocr` → `review ocr`, `reconcile` → `review orphans`, `supervise` → `watch supervise`).
6. **Docs.** Update `CLAUDE.md`, `architecture.md`, `README.md`, the `triage` skill's prompts, all references to `.meta/` and `storage/`.

There is no "validation period" — sidecars are gone after phase 1. Validation is by inspection of a few processed files using `dnd meta get` / `dnd meta cat` after phase 2.

## Test migration

- `tests/test_events_toaster.py` — rewrite events fixture against the table; toaster cursor becomes an integer.
- `tests/test_scanner.py` — drop "writes worklist file" assertions; assert returned `Worklist` shape.
- `tests/test_pipeline.py` — assert DB row state instead of sidecar file state.
- `tests/test_runtime.py` — update path expectations (`storage/` → `data/`).
- New `tests/test_db.py` — schema bootstrap, upsert, hash lookup, semilist round-trip, transaction batching.
- New `tests/test_meta_store.py` — `MetadataDoc` ↔ row mapping including OCR-table join.

## Doc updates

- `CLAUDE.md` — replace "every file gets a `.meta/<filename>.md` sidecar" paragraph with the DB story.
- `architecture.md` — flip the data-flow diagram. Metadata writes go to `data/dragndoc.db`, not next to the file.
- `README.md` — install/quickstart references to `storage/` → `data/`; remove sidecar examples.
- `.claude/skills/triage/SKILL.md` — change "read the sidecar" to "`dnd meta get <path>`".
- `.gitignore` — `storage/` → `data/`.

## Resolved

- **Phase 2 (dual-write safety net) — skipped.** Personal-tool scale, starting-from-scratch migration. Validation is ad-hoc inspection after phase 2.
- **Migration path — full scrub on day one.** No fallback corpus; phase 1 deletes every `.meta/` folder. Pipeline rebuilds from a blank DB.
- **`dnd grep` backing — FTS5 from day one.** `docs_fts` virtual table mirrors `title`/`summary`/`notes`/`tags`/`parties`. Triggers keep it synchronized. If FTS5 causes friction, the fallback is `LIKE` over the same TEXT columns — code change only, no schema migration.
- **Multi-value queries — FTS5 boolean operators or multi-clause LIKE AND.** Storage is sorted-on-write, but queries don't depend on order; `tags LIKE '%;a;%' AND tags LIKE '%;b;%'` works regardless, and FTS5 `MATCH 'a AND b'` is the natural form for richer queries.
- **Tag-membership performance — covered by FTS5.** No separate `doc_tags` table needed at this scale.
