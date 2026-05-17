"""LLM-assisted folder classifier — produces ``dir_mode`` proposals.

Given an unclassified directory, summarize its contents into a bounded
JSON payload, ask Ollama what kind of folder it is (collection / bundle
/ opaque / unknown), and enqueue a ``dir_mode`` proposal for user review
via ``dnd review proposals``.

The classifier never auto-commits — even high-confidence results pass
through ``dnd review`` per the always-propose principle. Falls back to
a ``source='fallback'`` proposal when Ollama is unreachable so the
queue always has something to review.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from dragndoc.config import get_settings
from dragndoc.log import get_logger


log = get_logger(__name__)


_VALID_MODES = {"collection", "bundle", "opaque", "unknown"}

_TEMPLATE_NAME_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}"),          # YYYY-MM-DD
    re.compile(r"\d{8}_\d{6}"),                 # YYYYMMDD_HHMMSS
    re.compile(r"IMG_\d+", re.I),               # IMG_1234
    re.compile(r"DSC[N]?_?\d+", re.I),          # DSC_1234 / DSCN1234
    re.compile(r"\[(?:[^\]]+)\]_\d+_\d{4}"),    # [name]_NUMBER_YYYY...
]


@dataclass
class DirProposalDraft:
    path: str
    mode: str
    rationale: str
    downstream_hints: dict[str, Any]
    source: str            # "llm" | "heuristic" | "fallback"


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def summarize_dir(path: Path, *, max_filenames: int = 10) -> dict[str, Any]:
    """Build a bounded JSON description of a directory's contents.

    Never reads file contents, only the directory listing. Bounded
    regardless of dir size: extension histogram, total size, a
    diverse-across-extensions sample of filenames, depth, parent mode.
    """
    if not path.exists() or not path.is_dir():
        return {"path": str(path), "exists": False}

    by_ext: Counter = Counter()
    total_size = 0
    samples_by_ext: dict[str, list[str]] = {}
    file_count = 0
    dir_count = 0

    try:
        for entry in path.iterdir():
            try:
                if entry.is_file():
                    file_count += 1
                    ext = entry.suffix.lower() or "<noext>"
                    by_ext[ext] += 1
                    try:
                        total_size += entry.stat().st_size
                    except OSError:
                        pass
                    samples_by_ext.setdefault(ext, []).append(entry.name)
                elif entry.is_dir():
                    dir_count += 1
            except OSError:
                continue
    except OSError as exc:
        log.warning("summarize_dir: scan failed for %s: %s", path, exc)
        return {"path": str(path), "exists": True, "error": str(exc)}

    # pick a sample of filenames spread across extensions
    sample: list[str] = []
    if samples_by_ext:
        per_bucket = max(1, max_filenames // max(1, len(samples_by_ext)))
        for ext, names in samples_by_ext.items():
            sample.extend(names[:per_bucket])
        sample = sample[:max_filenames]

    templated = any(
        any(pat.search(name) for pat in _TEMPLATE_NAME_PATTERNS)
        for name in sample
    )

    return {
        "path": str(path),
        "exists": True,
        "file_count": file_count,
        "subdir_count": dir_count,
        "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
        "extension_histogram": dict(by_ext.most_common()),
        "sample_filenames": sample,
        "filenames_appear_templated": templated,
    }


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


_CLASSIFY_PROMPT = """\
You are classifying a directory in a personal documents tree.

Given the summary below, decide whether the directory is:
- "collection": a folder of independent files where each is a standalone
  document (call recordings, scanned receipts, voice memos).
- "bundle": a folder of files that together form one document (a PDF + its
  supporting images + notes.txt). One file is typically primary.
- "opaque": a folder that should NOT be inventoried — vendor installer
  payloads, dependency caches, system folders, build outputs.
- "unknown": you can't tell.

Reply ONLY with strict JSON:
{
  "mode": "collection" | "bundle" | "opaque" | "unknown",
  "rationale": "one short sentence",
  "downstream_hints": {}
}

Directory summary:
"""


def _ollama_classify(summary: dict[str, Any]) -> dict | None:
    """Call Ollama with the summary; return the parsed dict or None on failure."""
    settings = get_settings()
    url = settings.ollama.url.rstrip("/") + "/api/generate"
    prompt = _CLASSIFY_PROMPT + json.dumps(summary, ensure_ascii=False, indent=2)
    body = {
        "model": settings.ollama.model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 512},
    }
    try:
        resp = requests.post(url, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("response") or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("dir_classifier: ollama call failed: %s", exc)
        return None

    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # cheap repair pass — strip code fences if any
        cleaned = raw.strip().strip("`").strip()
        try:
            return json.loads(cleaned)
        except Exception:  # noqa: BLE001
            log.warning("dir_classifier: response not parseable JSON: %s", raw[:200])
            return None


def classify(path: Path) -> DirProposalDraft:
    """Classify a directory. Always returns a draft (with source='fallback' on failure)."""
    summary = summarize_dir(path)
    parsed = _ollama_classify(summary)

    if parsed is None:
        return DirProposalDraft(
            path=str(path),
            mode="unknown",
            rationale="LLM unavailable or response unparseable",
            downstream_hints={},
            source="fallback",
        )

    mode = str(parsed.get("mode") or "unknown").lower()
    if mode not in _VALID_MODES:
        mode = "unknown"
    rationale = str(parsed.get("rationale") or "").strip() or "no rationale"
    hints = parsed.get("downstream_hints") or {}
    if not isinstance(hints, dict):
        hints = {}
    return DirProposalDraft(
        path=str(path),
        mode=mode,
        rationale=rationale,
        downstream_hints=hints,
        source="llm",
    )


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def enqueue_for(path: Path) -> int | None:
    """Classify a path and enqueue its proposal. Returns proposal id or None."""
    from dragndoc import proposals as proposals_mod
    from dragndoc.meta_store import relative_to_root

    draft = classify(path)
    try:
        rel = relative_to_root(path)
    except Exception:  # noqa: BLE001
        rel = str(path)
    return proposals_mod.enqueue(
        subject=proposals_mod.subject_for_dir(rel),
        kind=proposals_mod.KIND_DIR_MODE,
        value={
            "mode": draft.mode,
            "downstream_hints": draft.downstream_hints,
        },
        source=draft.source,
        rationale=draft.rationale,
    )
