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
2. Run `dnd triage count` (inbox-only by default). If the queue is empty
   but the user expects work, suggest `dnd digest` (which will populate
   the queue) or `dnd triage rebuild` (one-shot seed from existing rows).

Do NOT run `dnd doctor` at startup. It only matters for commands that
need Tesseract or Ollama (e.g. `dnd digest`). Run it lazily — just
before such a command, and only if that command fails in a way that
suggests a missing dependency.

## The triage queue

The pipeline maintains a `triage` queue table — every successful `dnd digest`
adds a row, every `/triage` filing removes one. You drain it via:

- `dnd triage list [--all] [--json]` — show pending entries (oldest first).
- `dnd triage next [--all]` — JSON for the oldest pending entry. Does NOT
   remove it; call `done` after you've filed the item. Collection entries expose
   `scope_kind="collection"`, `scope_path=<root-dir>`, and `member_count`.
- `dnd triage done <abs-path>` — remove a row or a tracked collection subtree
   from the queue (after `dnd mv`).
- `dnd triage count [--all]` — count pending entries.
- `dnd triage rebuild [--all]` — seed the queue from existing `docs` rows
  that aren't already queued (one-shot migration aid).

**Default scope is inbox-only.** Pass `--all` only in the two reorganisation
cases above.

## Drain loop

While `dnd triage count` > 0:

1. `dnd triage next` — JSON for the next entry. The payload contains the doc's
   `path`, `category`, `title`, `summary`, `confidence`, etc. For collection
   entries, treat `scope_path` as the filing target and `path` as only the
   representative leaf that supplied the summary.
2. If the summary is empty/unusable, run `dnd digest <abs-path>` to refresh,
   then re-pull `dnd triage next`. If `digest` fails with what looks like
   a missing dependency (Tesseract, Ollama, model), run `dnd doctor` to
   diagnose and report to the user. If still empty after a successful
   digest, ask the user via `AskUserQuestion`.

3. **Decide filing.**
   - Pick `category` from `taxonomy.md`. Use slash-separated form for nesting
     (e.g. `Financial/Receipts`).
   - Apply rules from `preferences.md` first; corrections.jsonl precedents
     second; LLM enrichment third.
   - Compose `smart_name` per `naming_convention` from preferences (default:
     `{date} - {correspondent} - {topic}.{ext}`).
   - If `scope_kind == "collection"`, make ONE filing decision for the
     collection root. Do not classify or rename individual leaves; the internal
     directory layout is preserved verbatim.

4. **Auto-apply gate.** Auto-apply only when ALL hold:
   - `confidence == high` (or `confirmed` if a human has signed off).
   - The chosen category exists in `taxonomy.md`.
   - The taxonomy hasn't been edited since the row was last digested
     (compare `taxonomy.md`'s mtime to the row's `digested` timestamp).

   Otherwise ask the user via `AskUserQuestion`.

5. **Apply.**
   - For ordinary docs, compute the destination path from the chosen category
     and smart filename, then run `dnd mv <src> <dst> [-f]`.
   - For `scope_kind == "collection"`, compute only the destination directory
     for the root, then run `dnd mv -y <scope_path> <category-dir>`. This moves
     the whole tree, preserves internal structure, and rewrites descendant DB
     paths together.
   - Never `mv` / `move` a file directly with the OS — the DB rows would be
     left pointing at the old path.

   For metadata-only edits use `dnd meta set <path> category=...`,
   `dnd meta edit <path>`, or `dnd meta apply <path> <file.md>`.

6. **Drain the queue.** After a successful move, run
   `dnd triage done <abs-dst-path>` to remove the entry. For collections, pass
   the moved root directory path, not a leaf. (`dnd rm` removes automatically
   via FK cascade; `dnd mv` does NOT — it just updates the path. The skill is
   responsible for calling `done`.)

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

## Scope

Triage handles ONLY digested documents in the queue. Do NOT run
`dnd scan` automatically — no end-of-session scan, no orphan sweep, no
OCR review pass. Those are separate, on-demand actions: only run them
if the user explicitly asks (e.g. "scan for orphans", "check OCR
candidates").

## Wrap up

Write a one-line summary to `memory/last-triage.json` with counts and
timestamp.
