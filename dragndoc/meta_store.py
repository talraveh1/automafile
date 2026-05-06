"""High-level metadata API used by pipeline / scanner / triage / CLI.

Wraps :mod:`dragndoc.db` with row-mapping, the markdown-frontmatter render/
parse used by ``dnd meta cat`` / ``meta edit`` / ``meta apply``, and the
translation from the LLM's :class:`~dragndoc.llm.EnrichmentResult` shape
into the new schema (folding ``correspondent`` into ``parties``,
``language`` into ``langs``, ``subcategory`` into a slash-separated
``category``, etc.).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from dragndoc.config import get_settings
from dragndoc.db import connect, from_semilist, to_semilist, transaction
from dragndoc.log import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_now_iso_micro() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


CONFIDENCE_LEVELS = ("low", "medium", "high", "confirmed")


@dataclass
class OcrInfo:
    decision: str = ""
    done: str | None = None
    engine: str | None = None
    engine_ver: str | None = None
    langs: list[str] = field(default_factory=list)

    def is_unset(self) -> bool:
        return not self.decision and not self.done and not self.engine

    def to_row(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "done": self.done,
            "engine": self.engine,
            "engine_ver": self.engine_ver,
            "langs": to_semilist(self.langs),
        }


@dataclass
class Doc:
    """One row in ``docs`` plus the matching row (if any) in ``ocr``."""

    path: str = ""
    hash: str = ""
    size: int = 0
    modified: str | None = None
    digested: str | None = None
    original: str = ""
    category: str = "Unknown"
    parties: list[str] = field(default_factory=list)
    langs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    date: str | None = None
    title: str | None = None
    confidence: str = "low"
    summary: str = ""
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    ocr: OcrInfo = field(default_factory=OcrInfo)

    # populated when read from DB
    id: int | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "hash": self.hash,
            "size": int(self.size),
            "modified": self.modified,
            "digested": self.digested,
            "original": self.original or self.path.rsplit("/", 1)[-1],
            "category": self.category or "Unknown",
            "parties": to_semilist(self.parties),
            "langs": to_semilist(self.langs),
            "tags": to_semilist(self.tags),
            "date": self.date,
            "title": self.title,
            "confidence": self.confidence if self.confidence in CONFIDENCE_LEVELS else "low",
            "summary": self.summary or "",
            "notes": self.notes or "",
            "extra": json.dumps(self.extra or {}, ensure_ascii=False),
        }


def _row_to_doc(row: sqlite3.Row) -> Doc:
    doc = Doc(
        id=row["id"],
        path=row["path"],
        hash=row["hash"],
        size=row["size"],
        modified=row["modified"],
        digested=row["digested"],
        original=row["original"],
        category=row["category"],
        parties=from_semilist(row["parties"]),
        langs=from_semilist(row["langs"]),
        tags=from_semilist(row["tags"]),
        date=row["date"],
        title=row["title"],
        confidence=row["confidence"],
        summary=row["summary"],
        notes=row["notes"],
        extra=_parse_json(row["extra"]),
    )
    keys = row.keys()
    if "ocr_decision" in keys and row["ocr_decision"] is not None:
        doc.ocr = OcrInfo(
            decision=row["ocr_decision"] or "",
            done=row["ocr_done"],
            engine=row["ocr_engine"],
            engine_ver=row["ocr_engine_ver"],
            langs=from_semilist(row["ocr_langs"]),
        )
    return doc


def _parse_json(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        result = json.loads(s)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


def relative_to_root(file_path: Path) -> str:
    """Return ``file_path`` as a forward-slash relative path under the docs root.

    Falls back to the absolute path string if the file lives outside the root.
    """
    settings = get_settings()
    try:
        return str(file_path.resolve().relative_to(settings.docs.resolve())).replace("\\", "/")
    except ValueError:
        return str(file_path).replace("\\", "/")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def get_by_path(rel_path: str) -> Doc | None:
    with connect(readonly=True) as conn:
        row = conn.execute("SELECT * FROM docs_full WHERE path = ?", (rel_path,)).fetchone()
    return _row_to_doc(row) if row else None


def get_by_file(file_path: Path) -> Doc | None:
    return get_by_path(relative_to_root(file_path))


def get_by_hash(hash_value: str) -> list[Doc]:
    with connect(readonly=True) as conn:
        rows = conn.execute("SELECT * FROM docs_full WHERE hash = ?", (hash_value,)).fetchall()
    return [_row_to_doc(r) for r in rows]


def all_docs() -> list[Doc]:
    with connect(readonly=True) as conn:
        rows = conn.execute("SELECT * FROM docs_full ORDER BY path").fetchall()
    return [_row_to_doc(r) for r in rows]


def upsert(doc: Doc) -> int:
    """Insert or update ``doc`` (keyed by ``path``); upsert OCR row too. Returns docs.id."""
    row = doc.to_row()
    with transaction() as conn:
        existing = conn.execute("SELECT id FROM docs WHERE path = ?", (row["path"],)).fetchone()
        if existing is None:
            conn.execute(_INSERT_DOC_SQL, row)
            doc_id = conn.execute("SELECT id FROM docs WHERE path = ?", (row["path"],)).fetchone()["id"]
        else:
            doc_id = existing["id"]
            conn.execute(_UPDATE_DOC_SQL, row)
        if not doc.ocr.is_unset():
            ocr_row = doc.ocr.to_row()
            ocr_row["doc_id"] = doc_id
            conn.execute(_UPSERT_OCR_SQL, ocr_row)
    doc.id = doc_id
    return doc_id


def update_path(old: str, new: str) -> None:
    """Rename a row's path (used by ``mv``/``filer``)."""
    with transaction() as conn:
        conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new, old))


def delete_by_path(rel_path: str) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM docs WHERE path = ?", (rel_path,))
    return cur.rowcount > 0


def mark_digested(rel_path: str, *, modified: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE docs SET digested = ?, modified = ? WHERE path = ?",
            (utc_now_iso_micro(), modified, rel_path),
        )


def has_metadata(file_path: Path) -> bool:
    rel = relative_to_root(file_path)
    with connect(readonly=True) as conn:
        row = conn.execute("SELECT 1 FROM docs WHERE path = ? LIMIT 1", (rel,)).fetchone()
    return row is not None


_INSERT_DOC_SQL = """
INSERT INTO docs (
    path, hash, size, modified, digested, original, category,
    parties, langs, tags, date, title, confidence, summary, notes, extra
) VALUES (
    :path, :hash, :size, :modified, :digested, :original, :category,
    :parties, :langs, :tags, :date, :title, :confidence, :summary, :notes, :extra
)
"""

_UPDATE_DOC_SQL = """
UPDATE docs SET
    hash = :hash,
    size = :size,
    modified = :modified,
    digested = :digested,
    original = :original,
    category = :category,
    parties = :parties,
    langs = :langs,
    tags = :tags,
    date = :date,
    title = :title,
    confidence = :confidence,
    summary = :summary,
    notes = :notes,
    extra = :extra
WHERE path = :path
"""

_UPSERT_OCR_SQL = """
INSERT INTO ocr (doc_id, decision, done, engine, engine_ver, langs)
VALUES (:doc_id, :decision, :done, :engine, :engine_ver, :langs)
ON CONFLICT(doc_id) DO UPDATE SET
    decision   = excluded.decision,
    done       = excluded.done,
    engine     = excluded.engine,
    engine_ver = excluded.engine_ver,
    langs      = excluded.langs
"""


# ---------------------------------------------------------------------------
# EnrichmentResult → Doc translation
# ---------------------------------------------------------------------------


def _file_modified_iso(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _category_with_subcategory(category: str | None, subcategory: str | None) -> str:
    cat = (category or "Unknown").strip() or "Unknown"
    sub = (subcategory or "").strip()
    return f"{cat}/{sub}" if sub else cat


def _confidence_value(value: str | None, review: bool | None) -> str:
    """Map LLM-side ``confidence`` string + ``review`` flag onto the 4-bucket enum."""
    raw = (value or "low").strip().lower()
    if raw not in CONFIDENCE_LEVELS:
        raw = "low"
    return raw


def _parties_from_correspondent(correspondent: str | None) -> list[str]:
    if not correspondent:
        return []
    parts = [p.strip() for p in re.split(r"[,;/]| and | & ", correspondent) if p.strip()]
    return parts or [correspondent.strip()]


def _langs_from_language(language: str | None) -> list[str]:
    if not language or language.lower() == "unknown":
        return []
    parts = [p.strip() for p in re.split(r"[,+;]", language) if p.strip()]
    return parts or [language.strip()]


def doc_from_enrichment(
    path: Path,
    *,
    enrichment: dict[str, Any],
    file_hash: str,
    ocr_info: OcrInfo,
    summary: str | None = None,
) -> Doc:
    """Build a fresh :class:`Doc` from enrichment + file metadata."""
    st = path.stat()
    extra: dict[str, Any] = {}
    for key in ("amount", "currency", "reason"):
        val = enrichment.get(key)
        if val not in (None, "", "null"):
            extra[key] = val

    return Doc(
        path=relative_to_root(path),
        hash=file_hash,
        size=st.st_size,
        modified=_file_modified_iso(path),
        digested=utc_now_iso_micro(),
        original=path.name,
        category=_category_with_subcategory(enrichment.get("category"), enrichment.get("subcategory")),
        parties=_parties_from_correspondent(enrichment.get("correspondent")),
        langs=_langs_from_language(enrichment.get("language")),
        tags=[str(t) for t in (enrichment.get("tags") or []) if t],
        date=enrichment.get("date") or None,
        title=enrichment.get("title") or None,
        confidence=_confidence_value(enrichment.get("confidence"), enrichment.get("review")),
        summary=(summary if summary is not None else enrichment.get("summary")) or "",
        notes="",
        extra=extra,
        ocr=ocr_info,
    )


# ---------------------------------------------------------------------------
# Markdown render / parse — for ``dnd meta cat`` / ``meta edit`` / ``meta apply``
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def to_markdown(doc: Doc) -> str:
    """Render a :class:`Doc` to YAML-frontmatter + Summary/Notes sections."""
    front = {
        "path": doc.path,
        "hash": doc.hash,
        "size": doc.size,
        "modified": doc.modified,
        "digested": doc.digested,
        "original": doc.original,
        "category": doc.category,
        "parties": list(doc.parties),
        "langs": list(doc.langs),
        "tags": list(doc.tags),
        "date": doc.date,
        "title": doc.title,
        "confidence": doc.confidence,
    }
    if not doc.ocr.is_unset():
        front["ocr"] = {
            "decision": doc.ocr.decision,
            "done": doc.ocr.done,
            "engine": doc.ocr.engine,
            "engine_ver": doc.ocr.engine_ver,
            "langs": list(doc.ocr.langs),
        }
    if doc.extra:
        front["extra"] = doc.extra

    yaml_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body = (
        "---\n"
        f"{yaml_text.rstrip()}\n"
        "---\n\n"
        "# Summary\n\n"
        f"{doc.summary or ''}\n\n"
        "# Notes\n\n"
        f"{doc.notes or ''}\n"
    )
    return body


def _split_body(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    parts = re.split(r"^# (Summary|Notes)\s*\n", text, flags=re.MULTILINE)
    summary, notes = "", ""
    if len(parts) >= 3:
        for i in range(1, len(parts), 2):
            heading = parts[i].strip().lower()
            body = parts[i + 1] if i + 1 < len(parts) else ""
            if heading == "summary":
                summary = body.strip()
            elif heading == "notes":
                notes = body.strip()
    return summary, notes


def doc_from_markdown(text: str, *, base: Doc | None = None) -> Doc:
    """Parse a markdown-frontmatter document into a :class:`Doc`.

    ``base``, when given, supplies values for fields the user didn't include
    in their edit (typically the immutable triplet ``hash``/``size``/``original``,
    plus any pipeline-managed timestamps).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter delimiter (---)")
    front_raw, body = match.group(1), match.group(2)
    try:
        front = yaml.safe_load(front_raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter YAML parse error: {exc}") from exc
    if not isinstance(front, dict):
        raise ValueError("frontmatter must be a YAML mapping")

    summary, notes = _split_body(body)
    seed = base or Doc()

    def _list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return from_semilist(value) if ";" in value else [value]
        if isinstance(value, Iterable):
            return [str(v) for v in value if v]
        return [str(value)]

    ocr_seed = front.get("ocr")
    ocr = seed.ocr
    if isinstance(ocr_seed, dict):
        ocr = OcrInfo(
            decision=str(ocr_seed.get("decision") or ""),
            done=ocr_seed.get("done"),
            engine=ocr_seed.get("engine"),
            engine_ver=ocr_seed.get("engine_ver"),
            langs=_list(ocr_seed.get("langs")),
        )

    extra_seed = front.get("extra")
    extra = extra_seed if isinstance(extra_seed, dict) else dict(seed.extra)

    raw_size = front.get("size")
    size_value = raw_size if raw_size is not None else (seed.size or 0)

    return Doc(
        id=seed.id,
        path=str(front.get("path") or seed.path),
        hash=str(front.get("hash") or seed.hash),
        size=int(size_value),
        modified=front.get("modified") or seed.modified,
        digested=front.get("digested") or seed.digested,
        original=str(front.get("original") or seed.original or ""),
        category=str(front.get("category") or seed.category or "Unknown"),
        parties=_list(front.get("parties", seed.parties)),
        langs=_list(front.get("langs", seed.langs)),
        tags=_list(front.get("tags", seed.tags)),
        date=front.get("date") or seed.date,
        title=front.get("title") or seed.title,
        confidence=str(front.get("confidence") or seed.confidence or "low"),
        summary=summary or seed.summary,
        notes=notes or seed.notes,
        extra=extra,
        ocr=ocr,
    )
