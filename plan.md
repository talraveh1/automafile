# Automafile v2 — Python-only rewrite, detailed implementation plan

## Context

The previous implementation built around Paperless-ngx is being retired. Paperless's "I own the files" model conflicts with the user's stated requirement: files must live in their own filesystem, be renameable/moveable outside any tool, and the metadata layer must be a helper, not a portal.

The replacement is a thin Python pipeline that watches a folder, extracts text + metadata from each file (using OCR only when needed), asks a local Ollama LLM for tags + category + summary, writes that information **into the file itself** (native metadata) when the format supports it, and falls back to a Markdown sidecar in a hidden `.meta/` subfolder otherwise. A separate Claude Code skill named `/triage` decides where each file is filed (folder + smart filename), informed by project-local memory of the user's preferences and prior corrections.

**This plan is for a new agent starting from a fresh empty directory.** Do not reuse anything from the existing `d:\automafile` repo — it has Docker, PowerShell, Paperless integration, and other obsolete leftovers. Reference it only to copy across the prompt template, the memory file shapes, and the `/triage` SKILL.md skeleton, all of which translate cleanly.

## Prerequisites

- Git installed.
- Python 3.12 or newer installed and on PATH (`python --version`).
- `winget` available (used optionally to install Tesseract).
- The user is on Windows 11 with NTFS, OneDrive sync active. The Inbox folder will be inside their OneDrive tree; pin it to "Always keep on this device" before relying on the pipeline.

## Additional installations required

- **Ollama** running locally at `http://localhost:11434` with the `aya-expanse:8b` model.
- **Tesseract OCR** installed on the host. Required language packs: `eng`, `heb`. Verify with `tesseract --list-langs`. Install via `winget install Tesseract-OCR` (or upstream installer) and add the `heb.traineddata` to the tessdata directory.

## Stack

- **Language:** Python 3.12.
- **Package manager:** standard `pip` + `venv`. No Poetry, no uv. Keep it boring and stdlib-friendly.
- **CLI framework:** `typer` (because Click is verbose, argparse is uglier).
- **File watching:** `watchdog`.
- **PDF text-layer extraction:** `pypdf`.
- **PDF metadata + page rendering:** `pikepdf` (also handles XMP) and `pdf2image` (for OCR rasterization, requires `poppler` binaries — bundle via `pdf2image[poppler]` or document the manual install).
- **Office docs:** `python-docx`, `openpyxl`, `python-pptx`.
- **Images (text + format support):** `Pillow`, `pillow-heif` (for HEIC), `pytesseract` (Tesseract wrapper).
- **Image metadata write:** `Pillow` for tags/title/description in EXIF/XMP; `piexif` for richer EXIF if needed.
- **HTML / EPUB:** `beautifulsoup4`, `ebooklib`.
- **Content sniffing for unknown extensions:** `python-magic-bin` (Windows-friendly libmagic).
- **YAML frontmatter:** `PyYAML`.
- **HTTP client (Ollama):** `requests`.
- **Schema/validation:** `pydantic` v2.
- **Windows toast:** `windows-toasts` (modern API; `win10toast` is dead). Falls back to console output if unavailable.
- **Tests:** `pytest`.
- **No Docker.** No Paperless. No Tika. No Gotenberg. No Postgres. No JVM. The only external services are Tesseract (a binary) and Ollama (already running).

## Project layout

```
<PROJECT_ROOT>/
├── README.md
├── pyproject.toml
├── .gitignore
├── .python-version
├── .env.example
├── CLAUDE.md                    # project-scoped Claude instructions
├── automafile/                  # the Python package
│   ├── __init__.py
│   ├── __main__.py              # enables `python -m automafile`
│   ├── cli.py                   # typer entry points
│   ├── config.py                # .env loader, paths, defaults
│   ├── log.py                   # logging setup, single source
│   ├── watcher.py               # watchdog observer
│   ├── dispatch.py              # extension/MIME → extractor router
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── base.py              # ExtractedDoc dataclass
│   │   ├── pdf.py
│   │   ├── docx.py
│   │   ├── xlsx.py
│   │   ├── pptx.py
│   │   ├── image.py
│   │   ├── html.py
│   │   ├── epub.py
│   │   ├── text.py
│   │   └── unknown.py
│   ├── ocr.py                   # decision logic + tesseract wrapper
│   ├── llm.py                   # Ollama client + tiered JSON parse fallback
│   ├── metadata/
│   │   ├── __init__.py
│   │   ├── schema.py            # pydantic models
│   │   ├── native.py            # native-metadata writers per format
│   │   ├── sidecar.py           # .md+YAML sidecar writer/reader
│   │   ├── mtime.py             # save/restore filesystem mtime
│   │   └── reconcile.py         # orphan detection, hash matching
│   ├── scanner.py               # tree walker → worklist
│   ├── notifier.py              # toast + console fallback
│   ├── filer.py                 # the actions /triage performs
│   └── prompts/
│       └── triage.txt           # the LLM system prompt template
├── memory/                      # gitignored
│   ├── pointer.md
│   ├── preferences.md
│   ├── taxonomy.md
│   └── corrections.jsonl        # empty file at seed time
├── .claude/
│   └── skills/
│       └── triage/
│           └── SKILL.md
├── scripts/
│   ├── install.ps1              # bootstrap helper
│   └── run-watcher.ps1          # tiny wrapper around `python -m automafile watch`
├── storage/                     # gitignored, runtime
│   ├── scan/                    # scan-<ts>.json, review-<ts>.json
│   └── logs/
├── tests/
│   ├── fixtures/                # sample PDFs, JPEGs, docx, etc.
│   ├── test_dispatch.py
│   ├── test_ocr_decision.py
│   ├── test_native_metadata.py
│   ├── test_sidecar.py
│   ├── test_reconcile.py
│   └── test_llm_parse.py
└── docs/
    └── architecture.md
```

## .gitignore

```
# venv
.venv/
__pycache__/
*.pyc

# secrets and runtime
.env
storage/

# Claude /triage memory (local-only learnings)
memory/

# OS
.DS_Store
Thumbs.db
```

## .env.example

```
# Ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=aya-expanse:8b

# paths (Windows-style; pipeline uses Path which handles them)
DOCUMENTS_ROOT=C:\Users\trax7\OneDrive\Documents
INBOX_DIR=Inbox
MANAGED_DIR=Automafile

# OCR
TESSERACT_LANGS=heb+eng
TESSERACT_BIN=                 # leave empty if on PATH; else absolute path

# behavior
WATCH_SETTLE_SECONDS=2
WATCH_POLLING_INTERVAL=5
LOG_LEVEL=INFO

# notifications
TOAST_PORT=8765                # listener port (currently unused; reserved for future host-side toast bridge)
```

## pyproject.toml essentials

- Project name `automafile`, version `0.1.0`.
- Console scripts: `automafile = "automafile.cli:app"` (so `automafile <subcommand>` works after `pip install -e .`).
- Pin `python_requires = ">=3.12"`.
- Dependencies as listed in the Stack section, with reasonable lower bounds.

## Setup (`scripts/install.ps1`)

The bootstrap script does, idempotently:

1. Create `.venv` in repo root if absent.
2. Activate it (or invoke its python directly).
3. Upgrade pip.
4. `pip install -e .` (editable install, picks up `pyproject.toml`).
5. Verify Tesseract: `tesseract --list-langs` includes `heb` and `eng`. If not, print install instructions; do not auto-install.
6. Verify Ollama: GET `${OLLAMA_URL}/api/tags`, ensure `${OLLAMA_MODEL}` is listed. If missing, instruct user to `ollama pull aya-expanse:8b`.
7. Copy `.env.example` to `.env` if `.env` doesn't exist.
8. Create `<DOCUMENTS_ROOT>/<INBOX_DIR>` and `<DOCUMENTS_ROOT>/<MANAGED_DIR>` if they don't exist.
9. Create `memory/` and seed templated files (see "Memory directory" below).
10. Print next-step instructions.

The script is intentionally PowerShell-thin; the heavy work lives in Python.

## Data model

### Sidecar (Markdown + YAML frontmatter)

For files where native metadata isn't possible. Lives at `<file_dir>/.meta/<filename>.<ext>.md`.

```markdown
---
schema_version: 1
content_hash: sha256:9f3b...
file_size: 14523
filename_at_creation: meeting-notes.txt
relative_path: Inbox/meeting-notes.txt
language: he
tags: [personal, notes]
category: Personal
correspondent: null
date: null
amount: null
currency: null
confidence: high
ocr:
  decision: never
  done_at: null
  engine: null
  engine_version: null
  languages: null
metadata_modified: 2026-05-01T10:22:33Z
metadata_modified_by: automafile-watcher 0.1.0
filed_at: null
filed_path: null
---

# Summary

Short, factual 1-3 sentence summary in the document's main language.

# Notes

(empty by default; reserved for human additions)
```

`relative_path` is relative to `DOCUMENTS_ROOT`, **not** absolute. This makes the whole tree portable.

### Native metadata mapping

Each format gets the same logical fields written into its own metadata block:

| Logical field | PDF (Info dict + XMP) | DOCX (core + custom) | XLSX/PPTX (core + custom) | JPEG/PNG (XMP/EXIF) |
|---|---|---|---|---|
| Tags | `Keywords` | `cp:keywords` | `cp:keywords` | `XMP:Subject` (list) |
| Category (single) | custom XMP `automafile:Category` | custom prop `Category` | custom prop `Category` | XMP `automafile:Category` |
| Title | `Title` | `cp:title` | `cp:title` | XMP `dc:title` |
| Summary | `Subject` (cap ~500 chars) or XMP `dc:description` | `cp:description` | `cp:description` | XMP `dc:description` / EXIF `ImageDescription` |
| Correspondent | XMP `automafile:Correspondent` | custom prop `Correspondent` | custom prop `Correspondent` | XMP `automafile:Correspondent` |
| Document date | `pdf:CreationDate` (only if absent) or XMP `automafile:DocumentDate` | custom prop `DocumentDate` | custom prop `DocumentDate` | XMP `automafile:DocumentDate` (do not touch EXIF DateTimeOriginal) |
| Confidence | XMP `automafile:Confidence` | custom prop `Confidence` | custom prop `Confidence` | XMP `automafile:Confidence` |
| Metadata modified | `pdf:ModDate` is preserved; instead use XMP `xmp:MetadataDate` | custom prop `MetadataModified` | custom prop `MetadataModified` | XMP `xmp:MetadataDate` |

`automafile:` is a custom XMP namespace declared once in `metadata/native.py`. Everything our tool owns goes there to avoid stomping on standard fields.

**Strict rule:** native writers MUST snapshot file mtime/atime *before* writing and restore them after, via `os.utime(path, ns=(atime_ns, mtime_ns))`. The file's content hash will change (acceptable; OneDrive will re-sync); the mtime stays the same so Explorer and most tooling see no change.

### Scanner output schema

`storage/scan/scan-<YYYYMMDD-HHMMSS>.json`:

```json
{
  "ran_at": "2026-05-01T10:22:33Z",
  "documents_root": "C:\\Users\\trax7\\OneDrive\\Documents",
  "tree_size": 1432,
  "files_seen": 1432,
  "skipped": 12,

  "files_needing_ocr": [
    {"relative_path": "Inbox/scan.pdf", "reason": "no_text_layer"},
    {"relative_path": "Inbox/photo.jpg", "reason": "image_format"}
  ],
  "files_needing_metadata": [
    {"relative_path": "Inbox/notes.txt", "format": "txt", "reason": "no_metadata_present"}
  ],
  "files_with_partial_metadata": [
    {"relative_path": "Inbox/contract.pdf", "missing_fields": ["category", "summary"]}
  ],
  "files_with_stale_metadata": [
    {"relative_path": "Inbox/contract.pdf", "metadata_modified": "2026-03-01",
     "file_modified": "2026-04-15", "delta_days": 60}
  ],
  "ocr_review_candidates": [
    {"relative_path": "Archive/old-receipt.jpg",
     "previous_engine": "tesseract 4.1", "previous_languages": "eng",
     "current_engine": "tesseract 5.3", "current_languages": "heb+eng"}
  ],
  "orphan_sidecars": [
    {"sidecar_relative_path": "Inbox/.meta/foo.pdf.md",
     "missing_path": "Inbox/foo.pdf",
     "hash_in_sidecar": "sha256:9f3b...",
     "matches_in_tree": ["Archive/foo.pdf"]}
  ],
  "unprocessable_files": [
    {"relative_path": "Inbox/encrypted.pdf", "reason": "pdf_encrypted"}
  ]
}
```

`ocr_review_candidates` is the **separate list** that requires user review — never auto-OCR'd, even when the engine/languages have changed. `/triage` (or a dedicated CLI command) shows them to the user; on approval, OCR runs; on decline, the metadata's `metadata_modified` is bumped so the doc isn't flagged again.

### Memory schema

(unchanged from the previous design — bring across as-is)

```
memory/
├── pointer.md         # one-line index
├── preferences.md     # human-edited rules
├── taxonomy.md        # current categories + subcategories
└── corrections.jsonl  # one JSON per line: {ts, doc_relative_path, field, before, after, reason}
```

`preferences.md` keys to support:
- `documents_root`, `inbox_dir`, `managed_dir`
- `paperless_after_filing` — replaced by `keep_inbox_copy: false` (no Paperless involved)
- `auto_apply: high_only` / `none`
- `new_category_threshold: 3`
- `prompt_on_drift: true`
- `naming_convention` — default `{date} - {correspondent} - {topic}.{ext}`, user-overridable

## Components — what each script does

### `automafile/config.py`

- Read `.env` via `python-dotenv` or stdlib parsing.
- Resolve all paths to absolute `Path` objects.
- Provide a frozen `Settings` dataclass / pydantic BaseSettings.
- Expose `documents_root`, `inbox_dir`, `managed_dir`, `meta_subfolder` (default `.meta`), `tesseract_langs`, `ollama_url`, `ollama_model`, etc.
- One source of truth — every other module imports from here.

### `automafile/dispatch.py`

- Function `extract(path: Path) -> ExtractedDoc`.
- Maps extension → extractor module, with `python-magic` fallback for unknown extensions.
- Returns a uniform `ExtractedDoc(text: str, native_metadata: dict, ocr_used: bool, ocr_decision: str)`.

### `automafile/extractors/*.py`

Each extractor:

- Returns `ExtractedDoc`.
- Reads native metadata where present (don't overwrite — only fill missing).
- Never modifies the source file.
- For PDFs: try `pypdf` text layer first; if `< 100` chars **or** if any page has `< 50` chars and `≥30%` of pages are sparse, recommend OCR (full or partial). Encrypted PDFs raise a typed exception caught by the dispatcher and routed to `unprocessable_files`.
- For images: always recommend OCR. The decision is made before extraction; the OCR module produces the text.
- For Office: read `cp:` properties + custom properties; native text via `python-docx` etc.
- For text/HTML/Markdown: direct read; no metadata block to read.

### `automafile/ocr.py`

- `pdf_ocr_decision(path) -> Literal["ocr_full", "ocr_pages", "no_ocr", "skip_encrypted"]` (plus the page list for `ocr_pages`).
- `run_ocr(path, langs="heb+eng") -> str` — for images: `pytesseract.image_to_string`; for PDFs: rasterize pages via `pdf2image`, OCR each, join.
- `record_ocr_metadata(meta, langs, engine_version, decision)` — fills the `ocr` block.

### `automafile/llm.py`

- `enrich(text: str, hints: dict) -> EnrichmentResult` where `hints` includes existing metadata, file path, mime, etc.
- Builds the prompt from `prompts/triage.txt` (a copy of the prompt that lived in the previous repo's `config/router-second-pass-prompt.txt`, adapted to the new schema).
- Sends to Ollama with `format: json` and the same options used previously (`temperature 0.1`, `num_ctx 8192`, `num_predict 1024`).
- Sanitizes the input: replace `"` → `״` only when adjacent to Hebrew chars (regex `(?<=\p{IsHebrew})"|"(?=\p{IsHebrew})`).
- Tiered JSON-parse fallback identical to the previous implementation:
  1. strict
  2. repair (escape unescaped `"` inside known string-valued keys: `title`, `summary`, `reason`)
  3. retry once with the prompt extended by "do not use double-quotes inside string values"
  4. per-field regex extraction (`"summary":"...escaped..."` etc.) — partial recovery
  5. placeholder result with `category=unknown`, `confidence=low`, `needs_review=true` so the file still lands in the metadata, never disappears.
- Returns the tier label so the watcher can log it.

### `automafile/metadata/native.py`

One writer per format. Each:

1. Snapshots `(atime_ns, mtime_ns)` of the file.
2. Opens / parses the file via the appropriate library.
3. Maps logical fields → format-specific keys per the table above.
4. Writes back to the file.
5. Restores `(atime, mtime)`.
6. Always populates `xmp:MetadataDate` (or the equivalent custom prop) with current UTC ISO timestamp.

If a write fails (corrupt file, locked by another process, encrypted), the writer raises a typed exception and the caller falls through to sidecar.

### `automafile/metadata/sidecar.py`

- Lives in `<file_dir>/.meta/<filename>.<ext>.md`.
- The `.meta/` folder is created with NTFS hidden attribute (`+h`); files inside are *not* hidden.
- Read: parse YAML frontmatter via `PyYAML`, body remains as Markdown.
- Write: format frontmatter deterministically (sorted keys, stable ordering) so diffs are clean. Body lines for `# Summary` and `# Notes` are preserved if present, replaced if not.
- Hash: SHA-256 of the *file content*, prefixed with `sha256:`.
- The sidecar is the source of truth for misfit formats; never partial.

### `automafile/metadata/mtime.py`

Tiny module with two helpers:

```python
def snapshot(path: Path) -> tuple[int, int]: ...
def restore(path: Path, snapshot: tuple[int, int]) -> None: ...
```

Both modules above use these.

### `automafile/metadata/reconcile.py`

- `find_orphans(documents_root) -> list[OrphanReport]` — scans for sidecars whose described file is missing.
- For each orphan, computes a SHA-256 index of all files in the tree (cached on `(mtime, size)`) to find hash matches.
- Returns the candidates; **does not** auto-relink. The decision is `/triage`'s.

### `automafile/scanner.py`

- Walks `documents_root` once.
- For each file, evaluates: needs OCR? has metadata? metadata stale? metadata partial? OCR config changed?
- Caches `(path, mtime, size, content_hash, ocr_engine, ocr_languages, metadata_modified)` in `storage/scan/.cache.json` so subsequent runs are fast.
- Re-hashes only files where `(mtime, size)` differs from cache.
- Emits a worklist JSON (schema above) and prints a one-line summary.
- CLI: `automafile scan` writes the worklist; `automafile scan --json` prints it instead.

### `automafile/watcher.py`

- `watchdog.observers.PollingObserver` (polling, because Windows-bind-mount + OneDrive events are unreliable through native APIs in some setups; polling is safe and our throughput is low).
- Watches `documents_root/inbox_dir` recursively.
- On create / move-into events: settle for `WATCH_SETTLE_SECONDS`, verify file size is stable (read twice), then process.
- Processing pipeline per file:
  1. Dispatch to extractor.
  2. If OCR is recommended, run OCR.
  3. Build hint dict and call `llm.enrich`.
  4. Write metadata: native if format supports, else sidecar.
  5. Log per-file line: `relative_path | ocr=<decision> | tier=<llm_tier> | category=<x> | duration=<ms>`.
  6. Best-effort fire toast.

### `automafile/filer.py`

The actions invoked by `/triage`:

- `propose_filing(meta) -> FilingProposal(category, subcategory, smart_name)` — uses the extracted metadata + memory + taxonomy to decide. Pure function; does not touch disk.
- `apply_filing(path, proposal)` — moves the file to `<documents_root>/<managed_dir>/<Category>[/<SubCategory>]/<smart_name>.<ext>`.
- Moves the `.meta/<filename>.md` sidecar alongside it (to the destination's `.meta/`).
- Re-anchors the sidecar's `relative_path` field to the new location.
- Re-hashes (file content unchanged, hash unchanged unless the move involved a re-encoding — which it doesn't).
- Updates `metadata_modified`, sets `filed_at` and `filed_path`.
- For native-metadata-bearing files, also updates the in-file `automafile:Category` and `xmp:MetadataDate`.
- Idempotent: if target exists with the same hash, no-op. If target exists with a different hash, raises `TargetCollision` for the caller to handle.

### `automafile/notifier.py`

- `notify(title, body)` — uses `windows-toasts` if installed; falls back to `print` if not.
- Debounce: if called more than once within `5s`, coalesce subsequent calls into one toast at the end of the burst.

### `automafile/cli.py`

Typer app with these commands:

| Command | Purpose |
|---|---|
| `automafile watch` | Start the watcher (foreground; user backgrounds it via Task Scheduler or NSSM). |
| `automafile process <path>` | Process a single file once. For debugging or manual re-runs. |
| `automafile ocr <path>` | Force OCR on a file regardless of decision. |
| `automafile scan` | Run scanner; write worklist to `storage/scan/scan-<ts>.json`. |
| `automafile review-ocr` | Walk `ocr_review_candidates` interactively; for each, ask y/n. On y: re-OCR. On n: bump `metadata_modified`. |
| `automafile reconcile` | Walk orphan sidecars interactively; for each, propose hash-matched relocation. |
| `automafile bootstrap` | Create memory templates, seed taxonomy if absent. |

Every command takes `--documents-root` to override the env, and `--dry-run` where it makes sense.

## Memory directory seeding

`scripts/install.ps1` (or `automafile bootstrap`) writes these once if absent. Use the previous repo's `d:\automafile\memory\` as a reference for content; structurally identical, just adjusted to drop Paperless-specific language.

- `memory/pointer.md` — one-line index, references the others.
- `memory/preferences.md` — templated free-form rules with the keys listed in "Memory schema" above. Default values pre-filled.
- `memory/taxonomy.md` — the seven default top-level categories (`Financial`, `Legal`, `Research`, `Teaching`, `Personal`, `Media`, `Unknown`-as-staging). Empty subcategory section.
- `memory/corrections.jsonl` — empty file.

## Project CLAUDE.md

```markdown
# Automafile project instructions

## Memory

Project-local memory lives in `memory/` (gitignored). When working on filing decisions, taxonomy, or `/triage` behavior, start by reading [memory/pointer.md](memory/pointer.md). Update [memory/preferences.md](memory/preferences.md) when the user expresses a durable filing preference; append to [memory/corrections.jsonl](memory/corrections.jsonl) whenever the user overrides a `/triage` proposal.

## Architecture

Files live in the user's filesystem under `<DOCUMENTS_ROOT>/<INBOX_DIR>` and `<DOCUMENTS_ROOT>/<MANAGED_DIR>`. The Python pipeline (under `automafile/`) extracts text, runs OCR when needed, calls Ollama for enrichment, and writes metadata into the file (native) or a `.meta/<filename>.md` sidecar. The `/triage` skill decides where each file is filed.

## Skill scope

`/triage` at `.claude/skills/triage/` is project-scoped — it knows about this repo's memory layout and the metadata schema, and isn't reusable elsewhere.
```

## /triage skill (`.claude/skills/triage/SKILL.md`)

Frontmatter (per the user's scaffold convention):

```markdown
---
name: triage
description: File documents from <DOCUMENTS_ROOT>/<INBOX_DIR> into <DOCUMENTS_ROOT>/<MANAGED_DIR>/<Category>/<smart-name> using project-local memory of preferences, taxonomy, and prior corrections. Auto-applies high-confidence decisions and asks the user for ambiguous ones.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Grep, AskUserQuestion
---
```

Workflow (mirrors the v1 skill in spirit; differences flagged):

1. **Setup** — read memory; load env; verify Ollama + Tesseract reachable; load latest `storage/scan/scan-*.json` if present (run scanner if older than 24h or if user passed `--rescan`).
2. **Drift / orphan review** — for orphan sidecars in the latest scan, propose relinks (hash-matched candidates) and apply user's choices.
3. **Build the queue** — files in `<INBOX_DIR>` (not yet filed) AND files flagged in scan worklist as `needs_metadata` / `partial_metadata` / `stale_metadata`. Sort by oldest-first.
4. **For each doc:**
   - Read its current metadata (native or sidecar).
   - If summary present and ≥100 chars → use it.
   - Else, read sibling sidecar's `# Summary` body if any.
   - Else, `automafile process <path>` to generate one in-place. (Calls back into the Python pipeline; cheap when OCR isn't needed.)
   - If still empty/unusable → ask the user for guidance.
5. **Decide** filing: `category`, optional `subcategory`, `smart_name`. Apply preferences-md rules and corrections.jsonl precedents.
6. **Auto-apply gate** — same four conditions as v1: `confidence-high`, no review-needed, category exists in taxonomy, taxonomy unchanged since enrichment.
7. **Apply** — call `automafile process` if needed to refresh metadata; then `automafile filer apply --path <path> --category <c> --subcategory <s> --name <smart>` (a thin Python entry point that invokes `filer.apply_filing`).
8. **Cluster + propose new categories** — same threshold rule (≥3 docs, single docs never spawn).
9. **Learn** — append corrections to `memory/corrections.jsonl`. Propose new `preferences.md` rules after 3 similar corrections.
10. **OCR review** — at end of session, surface `ocr_review_candidates` from the scan; for each, ask user; on yes, run `automafile ocr`; on no, bump `metadata_modified` so it doesn't reappear.
11. **Wrap up** — write `memory/last-triage.json`, single-line summary.

The skill talks to the pipeline via the `automafile` CLI for any state-changing action. It doesn't reach into Python internals.

## OCR decision logic — full detail

```python
def needs_ocr_for_pdf(path: Path) -> OcrDecision:
    text = pypdf_extract_text(path)
    if not text or len(text.strip()) < 100:
        return OcrDecision(action="ocr_full", reason="no_text_layer")

    per_page = chars_per_page_via_pypdf(path)
    sparse = [i for i, c in enumerate(per_page) if c < 50]
    if not sparse:
        return OcrDecision(action="no_ocr")
    if len(sparse) <= len(per_page) * 0.3:
        return OcrDecision(action="ocr_pages", pages=sparse)
    return OcrDecision(action="ocr_full", reason="majority_sparse")
```

Thresholds (`100`, `50`, `0.3`) live in `config.py`, overridable via env. They're starting points; tune from real misclassifications via `corrections.jsonl`.

For images: always `ocr_full`. For HEIC: convert via `pillow-heif` first.

For encrypted PDFs (`pikepdf` raises): emit `unprocessable_files`, do not auto-retry.

### Re-OCR review (separate from auto)

The scanner emits `ocr_review_candidates` when a file's recorded `ocr.engine_version` or `ocr.languages` differ from the current configured tooling. **This list is never auto-processed.** The user reviews via `automafile review-ocr` or via `/triage`. For each candidate the user picks:

- `Yes, redo OCR` → run OCR with current config; update metadata; bump `metadata_modified`.
- `No, leave it` → only bump `metadata_modified` so the file isn't surfaced on the next scan.
- `Skip for now` → no change; will reappear on next scan.

## mtime preservation — explicit rule

Every native-metadata writer:

```python
ns = (path.stat().st_atime_ns, path.stat().st_mtime_ns)
try:
    write_metadata_to_file(path, ...)
finally:
    os.utime(path, ns=ns)
```

Hash will change. mtime will not. OneDrive will re-sync the new bytes (this is desired behavior — the user wants metadata replicated). Explorer's "Date modified" column stays at the original.

## OneDrive specifics

- Document the requirement that `<DOCUMENTS_ROOT>/<INBOX_DIR>` and `<MANAGED_DIR>` are pinned to "Always keep on this device" before running the watcher. Put this in `README.md` quickstart.
- Sidecars and native-metadata writes both happen on local files, so OneDrive will sync them up correctly.
- Don't rely on NTFS Alternate Data Streams anywhere — OneDrive does not preserve them.

## Toast notification

Foreground watcher prints to console. When `windows-toasts` is installed, also pops a toast on each new file processed (debounced). No HTTP listener — there's no Docker container to bridge from. Direct process-to-toast, simple.

## Verification checklist

The implementing agent must demonstrate each of these end to end before declaring done:

1. **Bootstrap** — fresh clone, `.\scripts\install.ps1` runs cleanly, venv created, deps installed, Tesseract + Ollama verified, memory templates seeded, `Inbox` and `Automafile` dirs exist.
2. **Watcher happy path** — drop a Hebrew PDF with a text layer into `Inbox/`. Watcher fires, no OCR runs, summary written into the PDF's `Subject`, log line shows `tier=strict ocr=no_ocr`. Verify via `pdfinfo` (or `pikepdf` REPL) that the metadata is present.
3. **Watcher OCR path** — drop a Hebrew JPEG. Watcher runs Tesseract with `heb+eng`, summary written into XMP `dc:description`. Verify with `exiftool` or `pikepdf`.
4. **Sidecar path** — drop a `.txt` file with Hebrew content. Watcher writes `<file_dir>/.meta/<name>.txt.md` with frontmatter + body. Verify the `.meta/` folder has `+h` attribute (`attrib +h .meta`).
5. **mtime preserved** — record `Get-ItemProperty <pdf>` mtime before drop; after watcher writes metadata, mtime is unchanged.
6. **LLM tier fallback** — synthetically inject malformed JSON via a stub Ollama responder (or by setting model temperature to 1.0) and verify `tier=repair` or `tier=retry` is logged, never `tier=placeholder` for content-bearing files.
7. **Scanner** — `automafile scan` walks the tree and produces a worklist with the right shape. Manually delete a sidecar's referenced file; the next `scan` reports it as `orphan_sidecars` with hash matches if applicable.
8. **Re-OCR review surfaces** — change `TESSERACT_LANGS` from `heb+eng` to `heb`; next `scan` lists previously-OCR'd files in `ocr_review_candidates`, **not** in `files_needing_ocr`. Run `automafile review-ocr`, decline; verify `metadata_modified` bumped and the file no longer surfaces on subsequent scans.
9. **/triage end-to-end** — drop 3 docs, run `/triage` in Claude Code from the repo root. Two get auto-applied, one prompts. Files end up at `<MANAGED_DIR>/<Category>/<smart-name>.<ext>`. Sidecars (where applicable) follow into `.meta/`.
10. **Filesystem ownership check** — rename a filed file in Explorer. Run `automafile scan`; the file appears in `files_with_partial_metadata` (path mismatch) or `orphan_sidecars`. Run `automafile reconcile`; hash-based relink is proposed.
11. **OneDrive integrity** — verify a filed file syncs cleanly: change one line in its filename, watch OneDrive update; metadata in the file body stays intact (it survived the rename).

## Things explicitly *not* in scope

- Docker, docker-compose, container orchestration. None of it.
- Paperless-ngx, Paperless-AI, Tika, Gotenberg, Postgres, Redis. None.
- A web UI. The user invokes via Claude Code or CLI; that is the interface.
- An HTTP server inside the project. No webhooks. No listeners.
- Email ingestion, IMAP, scanner-to-folder devices. Out of scope for v0.
- Backup of the documents tree. The user owns OneDrive sync; we don't replicate.
- Multi-user support. Single user.
- Cross-machine synchronization of `memory/`. Local only; user can copy by hand if they care.

## Cross-references — copy from the previous repo verbatim, then adapt

These artifacts in `d:\automafile\` translate cleanly and should be the starting points (don't reinvent):

- **The LLM prompt** — `d:\automafile\config\router-second-pass-prompt.txt`. Copy to `automafile/prompts/triage.txt`. Trim Paperless-specific phrasing; keep the per-field rules (especially the summary length and Hebrew-friendly clauses).
- **Memory file shapes** — `d:\automafile\memory\preferences.md`, `taxonomy.md`, `pointer.md`. Copy and adapt to drop Paperless terminology (`paperless_after_filing` → `keep_inbox_copy`, etc.).
- **/triage SKILL.md** — `d:\automafile\.claude\skills\triage\SKILL.md`. Copy. Replace Paperless API calls with `automafile` CLI invocations and filesystem reads. Keep the structure (drift check, queue build, decide, gates, file, learn).
- **The tier-parser fallback shape** — `d:\automafile\scripts\sync-paperless-documents.ps1`'s `Invoke-OllamaDecision` and helpers. Port the algorithm to Python (it's ~100 lines of straightforward porting; the regex patterns and tier names map 1:1).
- **The Hebrew-adjacency quote sanitizer** — same file, `Sanitize-ExcerptForJsonModel`. Port directly.
- **The `medical/health → personal` category alias** in `ConvertTo-CategoryKey`. Same script. Port the whole alias map.

After copying these, do not import or run any other code from the old repo. The rest is obsolete.

## Order of work

Build in this order so you have a working pipeline at every checkpoint:

1. **Bootstrap + skeleton** — pyproject, venv, `automafile bootstrap`, empty CLI commands wired up, memory templates seeded.
2. **Extractors + dispatcher** — happy-path text extraction for PDF, DOCX, TXT, JPEG (no OCR yet, no LLM yet). Verify with `automafile process` on fixtures.
3. **OCR module** — text-layer detection, Tesseract integration, image and PDF OCR. Verify on fixtures.
4. **LLM client** — Ollama call + tier parser. Test offline against canned malformed JSON.
5. **Native metadata writers** — start with PDF and JPEG (highest value); add Office formats; mtime preservation throughout.
6. **Sidecar writer** — for the misfit formats.
7. **Watcher** — wire it all together with `windows-toasts` notifications. End-to-end test: drop file, see metadata land.
8. **Scanner** — tree walk, cache, worklist emission.
9. **Reconcile** — orphan detection, hash matching, interactive CLI.
10. **Re-OCR review** — surface candidates, interactive resolve.
11. **Filer** — file-move + sidecar move + metadata update.
12. **/triage skill** — port from old repo; wire to `automafile` CLI.
13. **Tests** — pytest covering each extractor, the dispatcher, the OCR decision matrix, the LLM tier fallback, the reconcile hash logic, the mtime restoration.
14. **Documentation** — README quickstart, `docs/architecture.md` with the data-flow diagram.

## Final notes for the implementing agent

- Don't add features that weren't discussed. The plan is the spec.
- Don't introduce a database. Filesystem is the truth.
- Don't introduce JSON-schema-validation runtime libraries beyond `pydantic`. Keep dependencies tight.
- Comments per the user's global coding instructions (`~/.claude/CLAUDE.md`): inline comments lowercase, no trailing periods, before semantic blocks; user-facing strings (CLI help, errors, log messages) in proper case.
- All code uses `pathlib.Path`, never string concatenation for paths.
- All timestamps in metadata are ISO 8601 UTC with `Z` suffix.
- All file I/O is binary where the format demands it; text I/O always specifies `encoding="utf-8"` explicitly.
- Test fixtures live in `tests/fixtures/`; use representative Hebrew + English samples for PDFs and JPEGs.
