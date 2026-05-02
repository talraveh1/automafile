"""Markdown + YAML frontmatter sidecar reader/writer."""

from __future__ import annotations

import ctypes
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from automafile.config import get_settings
from automafile.log import get_logger
from automafile.metadata.hashing import hash_file
from automafile.metadata.schema import MetadataDoc, utc_now_iso


log = get_logger(__name__)


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
# matches the rename suffix produced by ``_quarantine`` so callers / scanner
# can identify quarantined files (e.g. ``foo.txt.md.broken-20260502-074512``)
QUARANTINE_SUFFIX_RE = re.compile(r"\.broken-\d{8}-\d{6}$")


def sidecar_path_for(file_path: Path) -> Path:
    settings = get_settings()
    folder = file_path.parent / settings.meta_subfolder
    return folder / f"{file_path.name}.md"


def relative_to_root(file_path: Path) -> str:
    settings = get_settings()
    try:
        return str(file_path.relative_to(settings.documents_root)).replace("\\", "/")
    except ValueError:
        return str(file_path).replace("\\", "/")


def _hide_directory(folder: Path) -> None:
    if not folder.exists():
        return
    if platform.system() != "Windows":
        return
    try:
        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(folder))
        if attrs == -1:
            return
        if not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(folder), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not hide directory %s: %s", folder, exc)


def _format_frontmatter(meta: dict[str, Any]) -> str:
    return yaml.safe_dump(
        meta,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )


def _split_body(existing: str) -> tuple[str, str]:
    """Return ``(summary_section, notes_section)`` from an existing sidecar body."""
    if not existing:
        return "", ""
    summary, notes = "", ""
    parts = re.split(r"^# (Summary|Notes)\s*\n", existing, flags=re.MULTILINE)
    # parts looks like [pre, "Summary", body, "Notes", body, ...]
    if len(parts) >= 3:
        for i in range(1, len(parts), 2):
            heading = parts[i].strip()
            body = parts[i + 1] if i + 1 < len(parts) else ""
            if heading.lower() == "summary":
                summary = body.strip()
            elif heading.lower() == "notes":
                notes = body.strip()
    return summary, notes


def read(file_path: Path) -> tuple[MetadataDoc | None, str, str]:
    """Return ``(metadata_doc | None, summary_body, notes_body)``.

    A genuinely-missing sidecar returns ``(None, "", "")`` — the fast path.
    A sidecar that exists but cannot be parsed is **renamed aside**
    (``<name>.broken-<ts>``) so the next write doesn't silently clobber
    user edits, an ERROR is logged, and a notification fires; the function
    still returns ``(None, "", "")`` so callers continue. Once quarantined,
    subsequent calls hit the missing path naturally.
    """
    spath = sidecar_path_for(file_path)
    if not spath.exists():
        return None, "", ""
    raw = spath.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        _quarantine(spath, "no_frontmatter")
        return None, "", ""
    front_text, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(front_text) or {}
    except yaml.YAMLError as exc:
        _quarantine(spath, f"yaml_error: {exc}")
        return None, "", ""
    try:
        doc = MetadataDoc(**data)
    except Exception as exc:  # noqa: BLE001
        _quarantine(spath, f"schema_error: {exc}")
        return None, "", ""
    summary, notes = _split_body(body)
    return doc, summary, notes


def _quarantine(spath: Path, reason: str) -> None:
    """Rename a corrupt sidecar aside so the next write preserves user data."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = spath.with_name(f"{spath.name}.broken-{ts}")
    log.error("Sidecar %s corrupt (%s); moving aside to %s", spath, reason, backup.name)
    try:
        spath.rename(backup)
    except OSError as exc:
        log.error("Could not quarantine %s: %s", spath, exc)
        return
    # best-effort notification — lazy import to avoid spinning up the toaster
    # on cold paths and to dodge import cycles
    try:
        from automafile.notifier import notify
        notify("Sidecar quarantined", f"{spath.parent.parent.name}/{spath.name} ({reason})")
    except Exception as exc:  # noqa: BLE001
        log.debug("Notification failed for quarantine: %s", exc)


def is_quarantined(name: str) -> bool:
    return bool(QUARANTINE_SUFFIX_RE.search(name))


def write(
    file_path: Path,
    doc: MetadataDoc,
    summary_body: str,
    notes_body: str | None = None,
) -> Path:
    """Write a sidecar atomically and return its path."""
    spath = sidecar_path_for(file_path)
    spath.parent.mkdir(parents=True, exist_ok=True)
    _hide_directory(spath.parent)

    existing_summary = ""
    existing_notes = ""
    if spath.exists():
        try:
            _, existing_summary, existing_notes = read(file_path)
        except Exception:
            pass

    final_summary = summary_body or existing_summary
    final_notes = notes_body if notes_body is not None else existing_notes

    front = _format_frontmatter(doc.to_frontmatter_dict())
    body_parts = ["---", front.rstrip(), "---", "", "# Summary", "", final_summary or "", "", "# Notes", "", final_notes or ""]
    text = "\n".join(body_parts).rstrip() + "\n"

    tmp = spath.with_suffix(spath.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, spath)
    return spath


def build_meta_doc_for_new_file(
    file_path: Path,
    enrichment_dict: dict[str, Any],
    ocr_block: dict[str, Any] | None = None,
) -> MetadataDoc:
    """Compose a ``MetadataDoc`` from a freshly-extracted file + enrichment output."""
    st = file_path.stat()
    base = {
        "schema_version": 1,
        "content_hash": hash_file(file_path),
        "file_size": st.st_size,
        "filename_at_creation": file_path.name,
        "relative_path": relative_to_root(file_path),
        "metadata_modified": utc_now_iso(),
        "metadata_modified_by": "automafile-watcher 0.1.0",
    }
    base.update({k: v for k, v in enrichment_dict.items() if k != "summary"})
    if ocr_block:
        base["ocr"] = ocr_block
    return MetadataDoc(**base)


def update_relative_path(file_path: Path, new_path: Path) -> None:
    """Move/rewrite a sidecar so it sits next to ``new_path`` and points at it."""
    settings = get_settings()
    old_sidecar = sidecar_path_for(file_path)
    new_sidecar = sidecar_path_for(new_path)
    if not old_sidecar.exists():
        return
    new_sidecar.parent.mkdir(parents=True, exist_ok=True)
    _hide_directory(new_sidecar.parent)
    doc, summary, notes = read(file_path)
    if doc is None:
        os.replace(old_sidecar, new_sidecar)
        return
    try:
        rel = str(new_path.relative_to(settings.documents_root)).replace("\\", "/")
    except ValueError:
        rel = str(new_path).replace("\\", "/")
    doc.relative_path = rel
    doc.metadata_modified = utc_now_iso()
    write(new_path, doc, summary, notes)
    if old_sidecar != new_sidecar and old_sidecar.exists():
        old_sidecar.unlink()
