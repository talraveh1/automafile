# Automafile

A thin Python pipeline that watches a folder, extracts text + metadata from
each file (using OCR only when needed), asks a local Ollama LLM for tags +
category + summary, and writes that information **into the file itself**
(native metadata) when the format supports it, or into a Markdown sidecar in
a hidden `.meta/` subfolder otherwise. A separate Claude Code skill named
`/triage` decides where each file is filed.

## Quickstart

There are two ways to run Automafile: **native** (a venv on the host) or
**containerized** (Docker / Podman). Native is simpler; containerized
sandboxes the agent away from the rest of the host filesystem.

### Native

```powershell
# from a fresh clone, with system Python 3.12+ on PATH
python scripts\install.py

# edit config.jsonc if your defaults differ from the example
# pin <documents_root>\<inbox_dir> to "Always keep on this device" in OneDrive

# start the watcher
.\.venv\Scripts\python.exe -m automafile watch
```

### Containerized (Docker Desktop, Podman Desktop, or any Compose-compatible runtime)

```powershell
# copy the example compose file and edit the documents bind-mount path inside it
copy compose.example.yml compose.yml
notepad compose.yml

# build the image and start the watcher in the background
docker compose up -d --build

# follow the logs
docker compose logs -f

# stop the watcher
docker compose down
```

The container talks to the host's Ollama via `host.docker.internal:11434`
and bind-mounts only the project workspace and the documents folder —
nothing else from the host is reachable.

### Prerequisites

- Windows 11.
- **Native**: Python 3.12+ on PATH; Tesseract OCR (heb+eng); Ollama.
- **Containerized**: a Compose-compatible runtime (Docker Desktop or Podman
  Desktop); Ollama running on the host.

Run `python -m automafile doctor` (native) or `docker compose run --rm
automafile python -m automafile doctor` (container) after install to verify
everything is reachable.

> **Git Bash gotcha**: when invoking the container CLI from MSYS-based
> shells (Git Bash), absolute Linux-style paths like `/docs/Inbox/file.txt`
> get rewritten to `C:/Program Files/Git/docs/...`. Use `//docs/...` (double
> slash) or run from PowerShell / `docker compose exec` instead.

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
├── compose.yml                      # local Compose file (gitignored)
├── compose.example.yml              # Compose template
├── Dockerfile                       # container build recipe
├── .devcontainer/                   # VS Code Dev Containers config
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

No Paperless, no Tika, no Postgres, no JVM, no web UI, no HTTP listener.
The interface is the CLI and the Claude Code `/triage` skill.

In containerized mode, the Windows toast notifier is replaced by stdout
logs. Headless containers have no notification center to surface toasts to;
adding a host-side bridge isn't worth the cognitive cost for a personal
project.

See [plan.md](plan.md) for the full design and [architecture.md](architecture.md)
for the data-flow diagram.
