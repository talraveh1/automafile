---
name: triage
description: File documents from <documents_root>/<inbox_dir> into <documents_root>/<Category>[/<Subcategory>]/<smart-name> using project-local memory of preferences, taxonomy, and prior corrections. Auto-applies high-confidence decisions and asks the user for ambiguous ones.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Grep, AskUserQuestion
---

# /triage

This skill files documents from the user's inbox into the managed tree. It is
*project-scoped*; it knows about this repo's memory schema and the metadata
written by the `dragndoc` Python pipeline. Don't try to reuse it elsewhere.

## Setup

1. Read [memory/preferences.md](../../../memory/preferences.md),
   [memory/taxonomy.md](../../../memory/taxonomy.md), and the last 50 lines of
   [memory/corrections.jsonl](../../../memory/corrections.jsonl).
2. Use `Bash` to run `dnd doctor` and confirm Tesseract +
   Ollama are reachable. If either is missing, halt and explain.
3. Run `dnd scan --json` to get a fresh in-memory worklist. The scanner no
   longer writes JSON to disk; the `--json` flag prints the result so you
   can parse it directly.

## Drift / orphan review

For each entry in `missing_files`, run `dnd review orphans` (which proposes
hash-matched relinks). Pass `--yes-all` to auto-accept single-match relinks;
multi-match cases will prompt interactively.

## Build the queue

Files in `<INBOX_DIR>` (read directly) AND files flagged in the scan worklist
as `files_needing_metadata`, `files_with_partial_metadata`, or
`files_with_stale_metadata`. Sort by oldest filesystem mtime first.

## For each document

1. Read its metadata row via `dnd meta get <path>` — prints JSON for one
   file. Or `dnd meta cat <path>` for a markdown render with frontmatter.
   The DB is the only source of truth; every file the pipeline has
   touched has a row in `docs`.
2. If the summary is present and ≥100 chars, use it.
3. Otherwise run `dnd process <path>` to generate it.
4. If the summary is still empty/unusable, ask the user what the document is
   about via `AskUserQuestion`.

`process` with no path scans the tree and processes everything that needs
work. Each successful run sets the row's `digested` timestamp and `modified`
field (the file's mtime at digest time). Subsequent runs skip files whose
recorded `modified` covers the file's current mtime. Pass `--force` to redo
everything regardless. Failed files are not marked and retry on the next run.

## Decide filing

- Pick `category` from `taxonomy.md`. Use slash-separated form for nesting
  (e.g. `Financial/Receipts`).
- Apply rules from `preferences.md` first; corrections.jsonl precedents
  second; LLM enrichment third.
- Compose `smart_name` per `naming_convention` from preferences (default:
  `{date} - {correspondent} - {topic}.{ext}`).

## Auto-apply gate

Auto-apply only when ALL hold:

1. `confidence == high` (or `confirmed` if a human has signed off).
2. The chosen category exists in `taxonomy.md`.
3. The taxonomy hasn't been edited since the row was last digested (compare
   file mtime of `taxonomy.md` to the row's `digested` timestamp).

Otherwise, ask the user via `AskUserQuestion`.

## Apply

Compute the destination path from the chosen category and smart filename,
then run `dnd mv <src> <dst> [-f]`. It moves the file and updates the DB
row's `path` together; orphaned rows can be relinked later via
`dnd review orphans`.

Never `mv` / `move` a file directly with the OS — the DB row will be left
pointing at the old path. Always go through `dnd mv`.

To edit a single field on a row (e.g. correct the category), use
`dnd meta set <path> category=Financial/Receipts`. For broader edits open
`dnd meta edit <path>` (frontmatter editor) or apply a markdown file with
`dnd meta apply <path> <file.md>`.

## Cluster + propose new categories

If three or more documents in this run share a strong topical cluster that
isn't in the taxonomy, propose a new category to the user (single docs never
spawn one). On acceptance, edit `memory/taxonomy.md`.

## Learn

For every override the user makes, append a line to
`memory/corrections.jsonl`:

```json
{"ts": "2026-05-01T10:22:33Z", "doc_relative_path": "...", "field": "category", "before": "Personal", "after": "Financial", "reason": "..."}
```

After three similar corrections, propose a new rule for `preferences.md`.

## OCR review

At the end of the session, walk `ocr_review_candidates` from the scan. For
each: ask user; on yes, run `dnd review ocr --yes-all` scoped to that file
(or invoke interactively).

## Wrap up

Write a one-line summary to `memory/last-triage.json` with counts and
timestamp.
