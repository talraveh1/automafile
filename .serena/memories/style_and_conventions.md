# Style and conventions

`CLAUDE.md` (project root) and `~/.claude/CLAUDE.md` (the user's global rules) are the source of truth for coding conventions. Read those — don't restate them here.

## Things specific to this repo, beyond the global rules

- **Single-orchestrator pattern**: per-file processing always goes through `dragndoc.pipeline.process_file`. New entry points (CLI commands, tests, watcher events) should call it, not reimplement the extract → ocr → llm → write sequence.
- **Sidecars are the only metadata store**: every file gets a `<dir>/.meta/<filename>.md` sidecar; original documents are read-only to the pipeline. Don't reintroduce a native-metadata writer — the previous one was removed because it duplicated data, mutated user files, and made `python-docx`/`python-pptx` writes silent no-ops.
- **Tiered LLM parser must never raise**: `dragndoc.llm.parse_with_tiers` always returns an `EnrichmentResult`. The placeholder tier (`tier="placeholder"`) is the explicit fallback for unparseable LLM output. Don't add a sixth tier that throws.
- **Moves carry the sidecar**: any code path that relocates a file must also move its `.meta/<name>.md` (use `metadata.sidecar.update_relative_path` or go through the `dnd mv` / `filer-apply` CLI). Never call `shutil.move` on a sidecar-bearing file without it.
- **Hash identity**: `sha256:<hex>` (with prefix) is the canonical content identity used by reconcile and orphan detection. Use `metadata.hashing.hash_file`; don't roll your own.
