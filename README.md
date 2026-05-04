# Drag'n'Doc

A thin Python pipeline that watches a folder, extracts text + metadata from
each file (using OCR only when needed), asks a local Ollama LLM for tags +
category + summary, and writes that information **into the file itself**
(native metadata) when the format supports it, or into a Markdown sidecar in
a hidden `.meta/` subfolder otherwise. A separate Claude Code skill named
`/triage` decides where each file is filed.

## Quickstart

There are two ways to run Drag'n'Doc: **native** (a venv on the host) or
**containerized** (Docker / Podman). Native is simpler; containerized
sandboxes the agent away from the rest of the host filesystem.

### Native

```powershell
# from a fresh clone, with system Python 3.12+ on PATH
python scripts\install.py

# edit config.jsonc if your defaults differ from the example
# pin <documents_root>\<inbox_dir> to "Always keep on this device" in OneDrive

# start the watcher
.\.venv\Scripts\dnd.exe watch start --fg

# in a second terminal, start the toaster (renders Windows toasts from the events journal)
.\.venv\Scripts\dnd.exe toaster

# optional: register the toaster as a Windows scheduled task that auto-starts at logon
python scripts\toaster.py            # install / refresh
python scripts\toaster.py --status   # show the entry
python scripts\toaster.py --uninstall
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

# pause the watcher without stopping the container
docker compose exec dragndoc dnd watch stop

# resume the watcher
docker compose exec dragndoc dnd watch start

# stop the watcher
docker compose down
```

The compose stack bind-mounts the repo at `/workspace` and then overlays
`/workspace/.venv` with a named Docker volume. That keeps the container's
Linux venv separate from the host's Windows venv while preserving the same
`.venv` path in both environments and in VS Code.

The container now starts through a small supervisor: it runs the watcher by
default, keeps the container alive when you intentionally pause the watcher,
and exits non-zero if the watcher dies unexpectedly so Docker can restart it.
That same startup path is used in the VS Code devcontainer.

The toaster always runs on the host (it's tiny, has no LLM/OCR dependencies,
and needs the host's notification center). It tails
`storage/events.jsonl` — which the containerized pipeline writes through
the bind-mounted workspace — so toasts surface natively even when the
pipeline lives inside the container.

The container talks to the host's Ollama via `host.docker.internal:11434`
and bind-mounts only the project workspace and the documents folder —
nothing else from the host is reachable.

### Prerequisites

- Windows 11.
- **Native**: Python 3.12+ on PATH; Tesseract OCR (heb+eng); Ollama.
- **Containerized**: a Compose-compatible runtime (Docker Desktop or Podman
  Desktop); Ollama running on the host.

Run `.\.venv\Scripts\dnd.exe doctor` (native) or `docker compose run --rm
dragndoc dnd doctor` (container) after install to verify
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
| `dnd watch` | Show the watcher subcommands and options. |
| `dnd watch start` | Start or resume the watcher; `--fg` runs it in the foreground. |
| `dnd watch stop` | Pause the supervised background watcher without stopping the container. |
| `dnd watch status` | Show whether the supervised watcher is running, stopped, or idle. |
| `dnd toaster` | Tail the events journal and fire Windows toasts; hosts a tray icon (right-click → Triage / Log / Exit). `--no-tray` for headless. |
| `dnd process <path>` | Process a single file once. |
| `dnd ocr <path>` | Force OCR on a file. |
| `dnd scan` | Walk the tree and emit a worklist. |
| `dnd review-ocr` | Walk OCR review candidates interactively. |
| `dnd reconcile` | Walk orphan sidecars interactively. |
| `dnd bootstrap` | Seed config + memory + folders. |
| `dnd doctor` | Diagnose the environment. |
| `dnd mv <src> <dst>` | Move a file together with its sidecar; this is the filing action Claude uses after deciding the destination path. |

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
├── dragndoc/                      # the Python package
├── tests/                           # pytest
├── .claude/skills/triage/SKILL.md   # the Claude /triage skill
├── memory/                          # gitignored, project-local /triage memory
├── storage/                         # gitignored, runtime
│   ├── scan/                        # scan worklists + hash cache
│   ├── logs/                        # rolling logs
│   ├── tessdata/                    # optional local Tesseract trainedata
│   ├── events.jsonl                 # append-only event journal (pipeline → toaster)
│   └── toaster.cursor               # toaster's byte-offset bookmark into events.jsonl
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

See [plan.md](plan.md) for the full design and [architecture.md](architecture.md)
for the data-flow diagram.
