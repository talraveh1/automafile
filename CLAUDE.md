# Drag'n'Doc project instructions

## Memory

Project-local memory lives in `memory/` (gitignored). When working on filing
decisions, taxonomy, or `/triage` behavior, start by reading
[memory/pointer.md](memory/pointer.md). Update
[memory/preferences.md](memory/preferences.md) when the user expresses a
durable filing preference; append to
[memory/corrections.jsonl](memory/corrections.jsonl) whenever the user
overrides a `/triage` proposal.

## Architecture

Files live in the user's filesystem under `<docs>/<inbox>`
(drop zone) and `<docs>/<Category>[/<Subcategory>]/<smart-name>.<ext>`
(filed). The Python package (under `dragndoc/`) extracts text, runs
OCR when needed, calls Ollama for enrichment, and writes metadata to
**`data/dragndoc.db`** — a single SQLite file, never on OneDrive. Every
file with metadata gets a row in `docs` (plus an optional row in `ocr`).
Original documents are never modified. The `/triage` skill decides where
each file is filed; moves go through `dnd mv` so the row's `path` is
updated to follow the file. Read metadata with `dnd meta get <path>`
(JSON) or `dnd meta cat <path>` (markdown + frontmatter); search with
`dnd grep <query>` (FTS5).

## Run modes

The project runs either natively (a venv on the host, via
`scripts/install.py`) or containerized (Docker / Podman, via the included
`Dockerfile` + `compose.yml`). Both share the same package; container mode
bind-mounts the documents folder to `/docs`, sets `DOCS=/docs`,
and reaches host Ollama via `host.docker.internal`. Toasts are decoupled
from the pipeline: pipeline writes rows to the `events` table in the
same DB, and a separate `dnd toaster` process (always run on the host)
polls that table by id and renders Windows toasts.

## Skill scope

`/triage` at `.claude/skills/triage/` is project-scoped — it knows about this
repo's memory layout and the metadata schema, and isn't reusable elsewhere.

## Coding rules

See [architecture.md](architecture.md) for the data-flow diagram.
All code uses `pathlib.Path`, never string concatenation for paths. All
timestamps in metadata are ISO 8601 UTC with `Z` suffix.
