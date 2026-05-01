# Automafile

A thin Python pipeline that watches a folder, extracts text + metadata from
each file (using OCR only when needed), asks a local Ollama LLM for tags +
category + summary, and writes that information **into the file itself**
(native metadata) when the format supports it, or into a Markdown sidecar in
a hidden `.meta/` subfolder otherwise. A separate Claude Code skill named
`/triage` decides where each file is filed.

## Quickstart

```powershell
# from a fresh clone, with system Python 3.12+ on PATH
python scripts\install.py

# edit config.jsonc if your defaults differ from the example
# pin <documents_root>\<inbox_dir> to "Always keep on this device" in OneDrive

# start the watcher
.\.venv\Scripts\python.exe -m automafile watch
```

### Prerequisites

- Windows 11.
- **Python 3.12+** on PATH (`python --version`).
- [Ollama](https://ollama.com/download) running locally with the
  `aya-expanse:8b` model: `ollama pull aya-expanse:8b`.
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) with the
  `heb` and `eng` language packs.

Run `python -m automafile doctor` after install to verify everything is
reachable.

## Configuration

Settings live in [config.jsonc](config.jsonc) at the repo root, copied from
[config.example.jsonc](config.example.jsonc) on first run. Every key may be
overridden by an environment variable of the same name in upper-case form
(useful for tests and one-off runs).

The pipeline only reads/writes within two paths: this **workspace folder**
and the **`documents_root`** you configure. It never reaches outside.

## CLI

| Command | Purpose |
| --- | --- |
| `automafile watch` | Start the watcher in the foreground. |
| `automafile process <path>` | Process a single file once. |
| `automafile ocr <path>` | Force OCR on a file. |
| `automafile scan` | Walk the tree and emit a worklist. |
| `automafile review-ocr` | Walk OCR review candidates interactively. |
| `automafile reconcile` | Walk orphan sidecars interactively. |
| `automafile bootstrap` | Seed config + memory + folders. |
| `automafile doctor` | Diagnose the environment. |
| `automafile filer-apply` | Move a file into `<documents_root>/<category>/...` (used by `/triage`). |

## Layout

```
<repo>/
├── config.jsonc                     # local config (gitignored)
├── config.example.jsonc             # template
├── architecture.md                  # data-flow diagram
├── automafile/                      # the Python package
├── tests/                           # pytest
├── .claude/skills/triage/SKILL.md   # the Claude /triage skill
├── memory/                          # gitignored, project-local /triage memory
├── storage/                         # gitignored, runtime
│   ├── scan/                        # scan worklists + hash cache
│   ├── logs/                        # rolling logs
│   └── tessdata/                    # optional local Tesseract trainedata
└── build/                           # gitignored: pytest cache, coverage data
```

Files live in the user's filesystem under `<documents_root>/<inbox_dir>`
(drop zone) and `<documents_root>/<Category>[/<Subcategory>]/<smart-name>.<ext>`
(filed). The pipeline never reaches outside that tree.

## OneDrive note

Pin `<documents_root>\<inbox_dir>` to "Always keep on this device" before
relying on the watcher. Sidecars are regular Markdown files in a hidden
`.meta/` subfolder; OneDrive syncs them transparently.

## What it doesn't do

No Docker (yet), no Paperless, no Tika, no Postgres, no JVM, no web UI, no
HTTP listener. The interface is the CLI and the Claude Code `/triage` skill.

See [plan.md](plan.md) for the full design and [architecture.md](architecture.md)
for the data-flow diagram.
