# Project overview

For purpose, architecture, scope, and design rationale, read in this order:

1. `README.md` — quickstart + CLI surface + layout.
2. `plan.md` — full implementation spec; the source of truth for what's in/out of scope.
3. `architecture.md` — data flow diagram and per-module responsibilities.
4. `CLAUDE.md` — project rules (memory layout, path/timestamp invariants).

If the docs disagree with this memory, the docs win. Update or delete the memory.

## Things NOT in the docs that future-you should know

- The repo was rewritten from a previous Paperless-ngx-based implementation. `plan.md` calls out things "to *not* reuse." The implementation now diverges from `plan.md` in a few places (src/ layout, JSONC config instead of `.env`, no `MANAGED_DIR` — files are filed directly under `<documents_root>/<Category>/`); the README is the current truth.
- Tesseract `heb`/`eng` traineddata is downloaded to `storage/tessdata/` and pointed at via `tessdata_prefix` in `config.jsonc`, because the system-wide tessdata directory under `C:\Program Files\Tesseract-OCR\` is not user-writable from this environment.
- Pikepdf 10.5 emits a benign warning on every PDF write: "Update to xmp:MetadataDate will be overwritten." Cosmetic — the value still gets written.
