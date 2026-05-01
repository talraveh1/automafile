# Style and conventions

`CLAUDE.md` (project root) and `~/.claude/CLAUDE.md` (the user's global rules) are the source of truth for coding conventions. Read those — don't restate them here.

## Things specific to this repo, beyond the global rules

- **Single-orchestrator pattern**: per-file processing always goes through `automafile.pipeline.process_file`. New entry points (CLI commands, tests, watcher events) should call it, not reimplement the extract → ocr → llm → write sequence.
- **Native + sidecar coexistence**: when a format supports native metadata, write it AND a sidecar. The sidecar is the source of truth for `/triage`; the native metadata is for downstream tooling.
- **Tiered LLM parser must never raise**: `automafile.llm.parse_with_tiers` always returns an `EnrichmentResult`. The placeholder tier (`tier="placeholder"`) is the explicit fallback for unparseable LLM output. Don't add a sixth tier that throws.
- **mtime preservation is non-negotiable**: every native metadata writer wraps its work in `metadata.mtime.preserve_times(path)`. New format writers must do the same. The `tests/test_native_metadata.py` suite enforces this with `snapshot()` comparisons.
- **Hash identity**: `sha256:<hex>` (with prefix) is the canonical content identity used by reconcile and orphan detection. Use `metadata.hashing.hash_file`; don't roll your own.
