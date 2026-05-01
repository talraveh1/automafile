# Automafile project instructions

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
(filed). The Python package (under `automafile/`) extracts text, runs
OCR when needed, calls Ollama for enrichment, and writes metadata into the
file (native) or a `.meta/<filename>.md` sidecar. The `/triage` skill
decides where each file is filed.

## Skill scope

`/triage` at `.claude/skills/triage/` is project-scoped — it knows about this
repo's memory layout and the metadata schema, and isn't reusable elsewhere.

## Coding rules

See [architecture.md](architecture.md) for the data-flow diagram.
All code uses `pathlib.Path`, never string concatenation for paths. All
timestamps in metadata are ISO 8601 UTC with `Z` suffix.
