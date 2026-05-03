"""Find orphan sidecars and propose hash-matched relinks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.log import get_logger
from dragndoc.metadata.hashing import hash_file
from dragndoc.metadata.sidecar import sidecar_path_for, read as sidecar_read


log = get_logger(__name__)


CACHE_NAME = "hash-index.json"


@dataclass
class OrphanReport:
    sidecar_path: Path
    described_filename: str
    described_relative_path: str
    sidecar_hash: str | None
    matches_in_tree: list[Path] = field(default_factory=list)


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # any dot-prefixed component (covers .meta/ and any other hidden dirs)
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        yield path


def iter_sidecars(root: Path):
    meta_name = get_settings().meta_subfolder
    for path in root.rglob(meta_name + "/*.md"):
        if path.is_file():
            yield path


def _load_cache(scan_dir: Path) -> dict[str, Any]:
    cache_path = scan_dir / CACHE_NAME
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(scan_dir: Path, cache: dict[str, Any]) -> None:
    cache_path = scan_dir / CACHE_NAME
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(cache_path)


def build_hash_index(root: Path) -> dict[str, list[Path]]:
    """Return ``{sha256_hash: [path, ...]}`` for every regular file under root."""
    settings = get_settings()
    cache = _load_cache(settings.scan_dir)
    new_cache: dict[str, dict] = {}
    index: dict[str, list[Path]] = {}
    hashed = 0
    cached_hits = 0
    for f in iter_files(root):
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        key = str(f)
        cached = cache.get(key)
        if cached and cached.get("mtime_ns") == st.st_mtime_ns and cached.get("size") == st.st_size:
            h = cached["hash"]
            cached_hits += 1
        else:
            try:
                h = hash_file(f)
                hashed += 1
            except Exception:
                continue
        new_cache[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "hash": h}
        index.setdefault(h, []).append(f)
    _save_cache(settings.scan_dir, new_cache)
    log.debug("hash index built under %s: %d files (%d hashed, %d cache hits)", root, hashed + cached_hits, hashed, cached_hits)
    return index


def find_orphans(root: Path) -> list[OrphanReport]:
    """Identify sidecars whose target file is missing; suggest hash matches."""
    settings = get_settings()
    meta_name = settings.meta_subfolder
    hash_index = build_hash_index(root)

    orphans: list[OrphanReport] = []
    for sidecar in iter_sidecars(root):
        target_name = sidecar.name[:-3] if sidecar.name.endswith(".md") else sidecar.name
        target_path = sidecar.parent.parent / target_name
        if target_path.exists():
            continue
        doc, _, _ = sidecar_read(target_path)
        rel = doc.relative_path if doc else target_name
        h = doc.content_hash if doc else None
        matches = hash_index.get(h, []) if h else []
        orphans.append(OrphanReport(
            sidecar_path=sidecar,
            described_filename=target_name,
            described_relative_path=rel,
            sidecar_hash=h,
            matches_in_tree=list(matches),
        ))
    if orphans:
        log.info("find_orphans under %s: %d orphan sidecar(s)", root, len(orphans))
    else:
        log.debug("find_orphans under %s: no orphans", root)
    return orphans
