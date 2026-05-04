---
name: triage
description: File documents from <docs>/<inbox> into <docs>/<Category>[/<Subcategory>]/<smart-name> using project-local memory of preferences, taxonomy, and prior corrections. Auto-applies high-confidence decisions and asks the user for ambiguous ones.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Grep, AskUserQuestion
---

# /triage

This skill files documents from the user's inbox into the managed tree. It is
*project-scoped*; it knows about this repo's memory schema and the metadata
written by the `dragndoc` Python pipeline. Don't try to reuse it elsewhere.

## Default scope: inbox only

By default `/triage` ONLY handles files inside `<docs>/<inbox>`. Files
that already live elsewhere in the tree are left alone — you should reorganise
them only when:

1. The user explicitly asks to reorganise specific files (e.g.
   `/triage <path>` or "please move these out of `Personal`"), OR
2. The taxonomy itself changed (a new sub-category was added) and existing
   files now belong somewhere new.

Both cases use the same drain-the-queue flow below; you just widen the scope
to `--all` when invoking the queue commands.

## Setup

1. Read [memory/preferences.md](../../../memory/preferences.md),
   [memory/taxonomy.md](../../../memory/taxonomy.md), and the last 50 lines of
   [memory/corrections.jsonl](../../../memory/corrections.jsonl).
2. Use `Bash` to run `dnd doctor` and confirm Tesseract +
   Ollama are reachable. If either is missing, halt and explain.
3. Run `dnd triage count` (inbox-only by default). If the queue is empty
   but the user expects work, suggest `dnd digest` (which will populate
   the queue) or `dnd triage rebuild` (one-shot seed from existing rows).

## The triage queue

The pipeline maintains a `triage_queue` table — every successful `dnd digest`
adds a row, every `/triage` filing removes one. You drain it via:

- `dnd triage list [--all] [--json]` — show pending entries (oldest first).
- `dnd triage next [--all]` — JSON for the oldest pending entry. Does NOT
  remove it; call `done` after you've filed the file.
- `dnd triage done <abs-path>` — remove a row from the queue (after `dnd mv`).
- `dnd triage count [--all]` — count pending entries.
- `dnd triage rebuild [--all]` — seed the queue from existing `docs` rows
  that aren't already queued (one-shot migration aid).

**Default scope is inbox-only.** Pass `--all` only in the two reorganisation
cases above.

## Drift / orphan review

For each entry in `dnd scan --json`'s `missing_files`, run `dnd review orphans`
(which proposes hash-matched relinks). Pass `--yes-all` to auto-accept
single-match relinks; multi-match cases will prompt interactively.

NB: orphan/reconcile handling is currently being reworked; if it misbehaves,
flag it to the user and skip — don't try to patch it inside this flow.

## Drain loop

While `dnd triage count` > 0:

1. `dnd triage next` — JSON for the next entry. The payload contains the doc's
   `path`, `category`, `title`, `summary`, `confidence`, etc. You usually have
   everything you need without a separate `meta get` call.
2. If the summary is empty/unusable, run `dnd digest <abs-path>` to refresh,
   then re-pull `dnd triage next`. If still empty, ask the user via
   `AskUserQuestion`.

3. **Decide filing.**
   - Pick `category` from `taxonomy.md`. Use slash-separated form for nesting
     (e.g. `Financial/Receipts`).
   - Apply rules from `preferences.md` first; corrections.jsonl precedents
     second; LLM enrichment third.
   - Compose `smart_name` per `naming_convention` from preferences (default:
     `{date} - {correspondent} - {topic}.{ext}`).

4. **Auto-apply gate.** Auto-apply only when ALL hold:
   - `confidence == high` (or `confirmed` if a human has signed off).
   - The chosen category exists in `taxonomy.md`.
   - The taxonomy hasn't been edited since the row was last digested
     (compare `taxonomy.md`'s mtime to the row's `digested` timestamp).

   Otherwise ask the user via `AskUserQuestion`.

5. **Apply.** Compute the destination path from the chosen category and smart
   filename, then run `dnd mv <src> <dst> [-f]`. It moves the file and updates
   the DB row's `path` together. Never `mv` / `move` a file directly with the
   OS — the row would be left pointing at the old path.

   For metadata-only edits use `dnd meta set <path> category=...`,
   `dnd meta edit <path>`, or `dnd meta apply <path> <file.md>`.

6. **Drain the queue.** After a successful move, run
   `dnd triage done <abs-dst-path>` to remove the entry. (`dnd rm` removes
   automatically via FK cascade; `dnd mv` does NOT — it just updates the
   path. The skill is responsible for calling `done`.)

## Cluster + propose new categories

If three or more documents in this run share a strong topical cluster that
isn't in the taxonomy, propose a new category to the user (single docs never
spawn one). On acceptance, edit `memory/taxonomy.md`. After that, you may
need to widen scope to `--all` to reorganise existing files into the new
sub-category.

## Learn

For every override the user makes, append a line to
`memory/corrections.jsonl`:

```json
{"ts": "2026-05-01T10:22:33Z", "doc_relative_path": "...", "field": "category", "before": "Personal", "after": "Financial", "reason": "..."}
```

After three similar corrections, propose a new rule for `preferences.md`.

## OCR review

At the end of the session, run `dnd scan --json` and walk
`ocr_review_candidates`. For each: ask user; on yes, run
`dnd review ocr --yes-all` scoped to that file (or invoke interactively).

## Wrap up

Write a one-line summary to `memory/last-triage.json` with counts and
timestamp.
