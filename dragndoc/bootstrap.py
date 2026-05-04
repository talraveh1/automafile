"""Memory template seeder + directory creation + DB schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from dragndoc.config import REPO_ROOT, ensure_config_file, get_settings, reset_settings
from dragndoc.db import bootstrap_schema
from dragndoc.log import get_logger


log = get_logger(__name__)


_POINTER = """\
# Memory pointer

This directory is the project-local memory for Drag'n'Doc.

- [preferences.md](preferences.md) — user-edited rules.
- [taxonomy.md](taxonomy.md) — current categories and subcategories.
- [corrections.jsonl](corrections.jsonl) — append-only log of `/triage` overrides.
- [last-triage.json](last-triage.json) — single-line summary of the last triage run (overwritten each run).
"""

_PREFERENCES = """\
# Preferences

Edit these freely. The `/triage` skill reads them on every run.

```yaml
docs: C:\\Users\\trax7\\OneDrive\\Documents
inbox: Inbox
keep_inbox_copy: false
auto_apply: high_only
new_category_threshold: 3
prompt_on_drift: true
naming_convention: "{date} - {correspondent} - {topic}.{ext}"
```

## Filing rules

(Add free-form rules below; `/triage` will respect the durable ones.)
"""

_TAXONOMY = """\
# Taxonomy

The source of truth for filing. Add or remove freely.

Format: top-level bullets are categories; nested bullets are subcategories.
After any name, an em-dash followed by text is treated as a description of
the user's intent (use it whenever a name alone is ambiguous).

- Financial
- Legal
- Research
- Teaching
- Personal
- Media
- Unknown
"""


def bootstrap(*, force: bool = False) -> None:
    """Idempotent: ensure config file, data dirs, DB schema, documents tree, memory templates."""
    # write the config file BEFORE loading settings, so the cached Settings
    # reflect what's actually on disk (matters on a fresh clone where the
    # example template differs from the in-code defaults)
    if ensure_config_file():
        log.info("Wrote default config.jsonc from config.example.jsonc.")
        reset_settings()

    settings = get_settings()
    repo_root = settings.repo_root

    # data + build dirs
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (repo_root / "build").mkdir(parents=True, exist_ok=True)

    # DB: create file + schema if missing
    bootstrap_schema(settings.db_path)

    # documents tree (only the inbox; category folders appear on first filing)
    settings.docs.mkdir(parents=True, exist_ok=True)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)

    # memory dir
    mem = repo_root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    _seed(mem / "pointer.md", _POINTER, force)
    _seed(mem / "preferences.md", _PREFERENCES, force)
    _seed(mem / "taxonomy.md", _TAXONOMY, force)
    corrections = mem / "corrections.jsonl"
    if force or not corrections.exists():
        corrections.write_text("", encoding="utf-8")

    log.info("Bootstrap complete: %s", repo_root)


def _seed(path: Path, content: str, force: bool) -> None:
    if force or not path.exists():
        path.write_text(content, encoding="utf-8")
