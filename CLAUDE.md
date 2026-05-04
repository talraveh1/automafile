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

Files live in the user's filesystem under `<documents_root>/<inbox_dir>`
(drop zone) and `<documents_root>/<Category>[/<Subcategory>]/<smart-name>.<ext>`
(filed). The Python package (under `dragndoc/`) extracts text, runs
OCR when needed, calls Ollama for enrichment, and writes metadata to a
`.meta/<filename>.md` sidecar — every file gets one, regardless of format.
Original documents are never modified. The `/triage` skill decides where
each file is filed; moves go through `dnd mv` so the sidecar always travels
with the file.

## Run modes

The project runs either natively (a venv on the host, via
`scripts/install.py`) or containerized (Docker / Podman, via the included
`Dockerfile` + `compose.yml`). Both share the same package; container mode
bind-mounts the documents folder to `/docs`, sets `DOCUMENTS_ROOT=/docs`,
and reaches host Ollama via `host.docker.internal`. Toasts are decoupled
from the pipeline: it appends events to `storage/events.jsonl`, and a
separate `dnd toaster` process (always run on the host) tails the
journal and renders Windows toasts.

## Skill scope

`/triage` at `.claude/skills/triage/` is project-scoped — it knows about this
repo's memory layout and the metadata schema, and isn't reusable elsewhere.

## Coding rules

See [architecture.md](architecture.md) for the data-flow diagram.
All code uses `pathlib.Path`, never string concatenation for paths. All
timestamps in metadata are ISO 8601 UTC with `Z` suffix.
