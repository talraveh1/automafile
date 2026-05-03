# Codebase structure

For the canonical layout and data flow, read `README.md` (Layout section) and `architecture.md` (mermaid diagram + step-by-step flow). Don't restate them here.

## Things to know that aren't in the docs

- `dragndoc/pipeline.py` (`process_file`) is the single orchestrator — both `watcher.py` and the `process` CLI subcommand call it. New pipeline stages go here.
- The pipeline writes BOTH native metadata AND a sidecar when a format supports native (`metadata_target` becomes `"native+sidecar"`). The sidecar is the source of truth for `/triage`; native metadata is for downstream tools that don't read sidecars.
- `dragndoc/llm.py::parse_with_tiers` returns the tier label (`strict | repair | retry | regex | placeholder`) on the result so the watcher logs it. Never raises — placeholder ensures every file gets some metadata.
- `metadata/sidecar.py::write` is atomic (tmp + rename). Existing `# Summary` / `# Notes` body is preserved when the new write doesn't supply one.
- `extractors/pdf.py::extract()` populates `ExtractedDoc.per_page_chars`. The pipeline passes that to `ocr.pdf_ocr_decision(per_page_chars=...)` so the PDF is parsed once. When called without `per_page_chars` (e.g., from the scanner), `pdf_ocr_decision` falls back to its own pypdf parse.
- Encryption detection in `pdf_ocr_decision` uses **pikepdf** (its `PasswordError` is unambiguous), not pypdf (which raises a `DependencyError` about missing `cryptography` instead of a clear "encrypted" signal).
- Sidecars that fail to parse (no frontmatter / invalid YAML / schema mismatch) are **quarantined** by `metadata.sidecar.read` — renamed to `<name>.broken-<ts>` with an ERROR log + `notifier.notify` call. The function still returns `(None, "", "")` so callers continue uninterrupted; the next read on the same path hits the missing branch. The scanner surfaces these in `worklist.quarantined_sidecars`; `/triage` walks them in its review step.
- `dragndoc/config.py::parse_jsonc` is a tiny stdlib-only JSONC stripper (line + block comments + trailing commas, while protecting string contents). No third-party JSONC dep.
- The repo supports two run modes: **native venv** (`scripts/install.py` → `python -m dragndoc watch`) and **containerized** (`docker compose up -d` against the `Dockerfile` + `compose.yml`). Both share the same package; container mode bind-mounts the documents folder to `/docs`, sets `DOCUMENTS_ROOT=/docs` via env, and reaches host Ollama via `host.docker.internal:11434`. Toasts are dropped in container mode (no host notification bridge — deliberate cost decision).
- `compose.yml` is gitignored; `compose.example.yml` is the template (same pattern as `config.jsonc` / `config.example.jsonc`).
- `_resolve_tesseract_bin` validates that `settings.tesseract_bin` exists before honoring it. This matters when the host's `config.jsonc` (with a Windows path) is bind-mounted into a Linux container — the resolver falls through to `shutil.which("tesseract")` instead of returning a non-existent Windows path.
