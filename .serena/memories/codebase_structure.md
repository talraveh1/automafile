# Codebase structure

For the canonical layout and data flow, read `README.md` (Layout section) and `architecture.md` (mermaid diagram + step-by-step flow). Don't restate them here.

## Things to know that aren't in the docs

- `automafile/pipeline.py` (`process_file`) is the single orchestrator — both `watcher.py` and the `process` CLI subcommand call it. New pipeline stages go here.
- The pipeline writes BOTH native metadata AND a sidecar when a format supports native (`metadata_target` becomes `"native+sidecar"`). The sidecar is the source of truth for `/triage`; native metadata is for downstream tools that don't read sidecars.
- `automafile/llm.py::parse_with_tiers` returns the tier label (`strict | repair | retry | regex | placeholder`) on the result so the watcher logs it. Never raises — placeholder ensures every file gets some metadata.
- `metadata/sidecar.py::write` is atomic (tmp + rename). Existing `# Summary` / `# Notes` body is preserved when the new write doesn't supply one.
- `extractors/pdf.py` exposes both `extract()` and `per_page_char_counts()`. The OCR decision in `ocr.py` calls `pypdf` directly to avoid double-parsing.
- `automafile/config.py::parse_jsonc` is a tiny stdlib-only JSONC stripper (line + block comments + trailing commas, while protecting string contents). No third-party JSONC dep.
- The repo supports two run modes: **native venv** (`scripts/install.py` → `python -m automafile watch`) and **containerized** (`docker compose up -d` against the `Dockerfile` + `compose.yml`). Both share the same package; container mode bind-mounts the documents folder to `/docs`, sets `DOCUMENTS_ROOT=/docs` via env, and reaches host Ollama via `host.docker.internal:11434`. Toasts are dropped in container mode (no host notification bridge — deliberate cost decision).
- `compose.yml` is gitignored; `compose.example.yml` is the template (same pattern as `config.jsonc` / `config.example.jsonc`).
- `_resolve_tesseract_bin` validates that `settings.tesseract_bin` exists before honoring it. This matters when the host's `config.jsonc` (with a Windows path) is bind-mounted into a Linux container — the resolver falls through to `shutil.which("tesseract")` instead of returning a non-existent Windows path.
