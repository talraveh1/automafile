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
3. Look for the latest `storage/scan/scan-*.json`. If older than 24h or absent,
  run `dnd scan`.

## Drift / orphan review

For each entry in `orphan_sidecars`, propose the hash-matched relocation if
exactly one candidate is present. If multiple candidates, ask the user via
`AskUserQuestion`. Apply chosen relinks via `dnd reconcile
--yes-all` (single-match) or by editing the sidecar's `relative_path` field
manually for multi-match cases.

## Quarantine review

For each entry in `quarantined_sidecars` (corrupt sidecars that were moved
aside during a previous read), ask the user via `AskUserQuestion`:

- **Restore**: rename the `.broken-<ts>` file back to its original name; the
  user fixes it manually before the next run.
- **Discard**: delete the broken file. The next pipeline run regenerates
  fresh metadata.
- **Skip**: leave it; will resurface on the next scan.

## Build the queue

Files in `<INBOX_DIR>` (read directly) AND files flagged in the scan worklist
as `files_needing_metadata`, `files_with_partial_metadata`, or
`files_with_stale_metadata`. Sort by oldest filesystem mtime first.

## For each document

1. Read its sidecar metadata via `dnd inspect <path>` —
   prints JSON for the file (or for the whole inbox when called with no
   path). Sidecars are the only source of truth; every file the pipeline
   has touched has one.
2. If the summary is present and ≥100 chars, use it.
3. Otherwise run `dnd process <path>` to generate it
   in-place.
4. If the summary is still empty/unusable, ask the user what the document is
   about via `AskUserQuestion`.

`process` over a worklist (`dnd process` with no path, or
`process <scan-*.json>`) marks each successfully-processed entry with a
``processed`` ISO timestamp and rewrites the worklist atomically (flush +
fsync + rename) after every file. Subsequent runs skip entries whose
``processed`` mark is at-or-after the file's current mtime. Pass
``--force`` to redo everything regardless. Failed files are not marked
and will be retried on the next run.

## Decide filing

- Pick `category` from `taxonomy.md`.
- Apply rules from `preferences.md` first; corrections.jsonl precedents
  second; LLM enrichment third.
- Pick `subcategory` only if the taxonomy lists one for this category.
- Compose `smart_name` per `naming_convention` from preferences (default:
  `{date} - {correspondent} - {topic}.{ext}`).

## Auto-apply gate

Auto-apply only when ALL hold:

1. `confidence == high`.
2. `review == false`.
3. The chosen category exists in `taxonomy.md`.
4. The taxonomy hasn't been edited since the enrichment was generated
   (compare file mtime of `taxonomy.md` to `metadata_modified` in the
   sidecar).

Otherwise, ask the user via `AskUserQuestion`.

## Apply

Compute the destination path from the chosen category, optional subcategory,
and smart filename, then run `dnd mv <src> <dst> [-f]`. It moves the file and
its sidecar together and fails if either target exists, unless `-f`.

Never `mv` / `move` a file directly with the OS — the sidecar will be left
behind. Always go through `dnd mv`.

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
each: ask user; on yes, run `dnd review-ocr --yes-all`
scoped to that file (or invoke directly). On no, the same command bumps
`metadata_modified` so the file doesn't reappear.

## Wrap up

Write a one-line summary to `memory/last-triage.json` with counts and
timestamp.
