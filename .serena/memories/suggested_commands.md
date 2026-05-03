# Suggested commands

The full CLI surface and quickstart commands live in `README.md`. Read those first.

## What's not in README

### Test commands

```powershell
.\.venv\Scripts\python.exe -m pytest                 # all tests (fast, ~2-3s)
.\.venv\Scripts\python.exe -m pytest -x              # stop at first failure
.\.venv\Scripts\python.exe -m pytest --cov=dragndoc --cov-report=term-missing
```

`build/pytest_cache/` and `build/.coverage` collect under `build/`; both are gitignored.

### Linting / formatting / type-checking

None configured. If you add one, declare it in `pyproject.toml` and document it in `README.md` — don't put it in this memory.

### Inspect metadata after writes (quick one-liners)

```powershell
# PDF docinfo
.\.venv\Scripts\python.exe -c "import pikepdf; p=pikepdf.open(r'<path>'); print(dict(p.docinfo))"

# JPEG EXIF
.\.venv\Scripts\python.exe -c "from PIL import Image; print(dict(Image.open(r'<path>').getexif()))"
```

### After completing a code change

1. Run `python -m pytest` — must be green.
2. If you touched `config.py`, `ocr.py`, or `llm.py`: run `python -m dragndoc doctor`.
3. If you touched extractors or metadata writers: drop a representative file into a temp inbox (set `DOCUMENTS_ROOT` env var or edit `config.jsonc`) and run `python -m dragndoc process <path>` end-to-end.

### Container commands

```powershell
docker compose up -d --build           # build image + start watcher
docker compose logs -f                 # follow logs
docker compose exec dragndoc bash    # interactive shell inside the container
docker compose run --rm dragndoc python -m dragndoc doctor   # one-shot
docker compose down                    # stop
docker compose down -v && docker compose up -d --build   # factory reset
```

**Git Bash gotcha**: absolute Linux paths like `/docs/Inbox/file.txt` get rewritten to `C:/Program Files/Git/docs/...` by MSYS path conversion. Use `//docs/...` or run from PowerShell.

### Test isolation

Tests use env-var overrides (`DOCUMENTS_ROOT`, `INBOX_DIR`, `LOG_LEVEL`) on top of `config.jsonc`. The `isolated_env` fixture in `tests/conftest.py` sets them and calls `reset_settings()`.

### Windows-specific gotchas

- `ls -Recurse` on the OneDrive Documents tree is slow and can stall on cloud-only files. Pin the relevant subtrees to "Always keep on this device" first.
- Path separators in `config.jsonc` are JSON strings, so `\` must be escaped as `\\` (e.g. `"C:\\Users\\..."`).
