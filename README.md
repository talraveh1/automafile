# Drag'n'Doc

A thin Python pipeline that watches a folder, extracts text + metadata from each file (using OCR when needed), asks a local Ollama LLM for tags + category + summary, and stores the result in a local SQLite database (`data/dragndoc.db`). A separate Claude Code skill named `/triage` decides where each file is filed.

## Quickstart

There are two ways to run Drag'n'Doc: **native** (a venv on the host) or **containerized** (Docker / Podman). Native is simpler; containerized sandboxes the agent away from the rest of the host filesystem.

### Native

```powershell
# from a fresh clone, with system Python 3.12+ on PATH
python scripts\install.py

# edit config.jsonc if your defaults differ from the example
# pin <docs>\<inbox> to "Always keep on this device" in OneDrive

# start the watcher
dnd watch start --fg

# start the toaster (polls the events table and renders Windows toasts)
dnd toaster start            # background; --fg to run in this terminal
dnd toaster status           # is it running? install state?
dnd toaster stop

# optional: auto-start the toaster at user logon (drops a Startup shortcut + AUMID)
dnd toaster install
dnd toaster uninstall
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

The compose stack bind-mounts the repo at `/workspace` and then overlays `/workspace/.venv` with a named Docker volume. That keeps the container's Linux venv separate from the host's Windows venv while preserving the same `.venv` path in both environments and in VS Code.

The container now starts through a small supervisor: it runs the watcher by default, keeps the container alive when you intentionally pause the watcher, and exits non-zero if the watcher dies unexpectedly so Docker can restart it. That same startup path is used in the VS Code devcontainer.

The toaster always runs on the host (it's tiny, has no LLM/OCR dependencies, and needs the host's notification center). It polls the `events` table in `data/dragndoc.db` — which the containerized pipeline writes through the bind-mounted workspace — so toasts surface natively even when the pipeline lives inside the container.

The container talks to the host's Ollama via `host.docker.internal:11434` and bind-mounts only the project workspace and the documents folder — nothing else from the host is reachable.

### Prerequisites

- Windows 11.
- Ollama running on the host machine.
- **Native**: Python 3.12+ on PATH; Tesseract OCR (heb+eng).
- **Containerized**: a Compose-compatible runtime (Docker, Podman).

Run `dnd doctor` (native) or `docker compose run --rm dragndoc dnd doctor` (container) after install to verify everything is reachable.

> **Git Bash gotcha**: when invoking the container CLI from MSYS-based shells (Git Bash), absolute Linux-style paths like `/docs/Inbox/file.txt` get rewritten to `C:/Program Files/Git/docs/...`. Use `//docs/...` (double slash) or run from PowerShell / `docker compose exec` instead.

## Configuration

Settings live in [config.jsonc](config.jsonc) at the repo root, copied from [config.example.jsonc](config.example.jsonc) on first run. Every key may be overridden by an environment variable of the same name in upper-case form (useful for tests and one-off runs).

The pipeline only reads/writes within two paths: this **workspace folder** and the **`docs`** you configure. It never reaches outside.

## CLI

| Command                             | Purpose                                                                      |
|-------------------------------------|------------------------------------------------------------------------------|
| `dnd watch`                         | Show the watcher subcommands and options.                                    |
| `dnd watch start`                   | Start or resume the watcher; `--fg` runs it in the foreground.               |
| `dnd watch stop`                    | Pause the supervised background watcher without stopping the container.      |
| `dnd watch status`                  | Show whether the supervised watcher is running, stopped, or idle.            |
| `dnd watch supervise`               | Container supervisor; owns the watcher process.                              |
| `dnd toaster start [--fg]`          | Start the toaster (background by default; `--fg` runs in this terminal).     |
| `dnd toaster stop`                  | Stop the running toaster.                                                    |
| `dnd toaster restart`               | Restart the running background toaster.                                      |
| `dnd toaster status`                | Show whether the toaster is running plus install state.                      |
| `dnd toaster install`               | Install Startup shortcut + register the AUMID for auto-start at logon.       |
| `dnd toaster uninstall`             | Remove the Startup shortcut + unregister the AUMID.                          |
| `dnd digest [path]`                 | Digest a single file or scan the tree and digest anything that needs work.   |
| `dnd ocr <path>`                    | Force OCR on a file and print recovered text.                                |
| `dnd scan`                          | Walk the tree and report what `digest` would do. No files are written.       |
| `dnd review ocr`                    | Walk OCR-drift candidates interactively.                                     |
| `dnd review orphans`                | Walk DB rows whose file is missing; offer hash-matched relinks.              |
| `dnd grep <pattern>`                | FTS5 search across `title`/`summary`/`notes`/`tags`/`parties`.               |
| `dnd meta get <path>`               | JSON dump of one row.                                                        |
| `dnd meta cat <path>`               | Markdown render (frontmatter + Summary + Notes).                             |
| `dnd meta set <path> field=value …` | Set one or more fields.                                                      |
| `dnd meta apply <path> <file.md>`   | Update a row from a markdown file (frontmatter+body).                        |
| `dnd meta edit <path>`              | Open the row's markdown in `$EDITOR`; apply on save.                         |
| `dnd ls / mv / cp / rm`             | DB-aware filesystem ops; `mv` updates the row's `path`.                      |
| `dnd bootstrap`                     | Seed config + memory + folders, create the DB.                               |
| `dnd doctor`                        | Diagnose the environment.                                                    |

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
├── dragndoc/                        # the Python package
├── tests/                           # pytest
├── .claude/skills/triage/SKILL.md   # the Claude /triage skill
├── memory/                          # gitignored, project-local /triage memory
├── data/                            # gitignored, runtime; never on OneDrive
│   ├── dragndoc.db                  # SQLite: docs · ocr · events · docs_fts · schema_meta
│   ├── runtime/                     # watcher pid + disabled flag
│   ├── logs/                        # rolling logs
│   ├── tessdata/                    # optional local Tesseract traineddata
│   └── toaster.cursor               # toaster's last-seen events.id
└── build/                           # gitignored: pytest cache, coverage data
```

Files live in the user's filesystem under `<docs>/<inbox>` (drop zone) and `<docs>/<Category>[/<Subcategory>]/<smart-name>.<ext>` (filed). The pipeline never reaches outside that tree.

## OneDrive note

Pin `<docs>\<inbox>` to "Always keep on this device" before relying on the watcher. The metadata DB at `data/dragndoc.db` is local only — never put `data/` on OneDrive (SQLite + sync providers don't mix safely). Back up `data/` independently of the documents tree.

## What it doesn't do

No Paperless, no Tika, no Postgres, no JVM, no web UI, no HTTP listener. The interface is the CLI and the Claude Code `/triage` skill. See [architecture.md](architecture.md) for the data-flow diagram.
