"""End-to-end per-file digest: extract → enrich → upsert metadata row."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.dispatch import extract as dispatch_extract
from dragndoc.extractors._caps import CapConfig, trim_to_word_boundary
from dragndoc.extractors.base import (
    EncryptedDocumentError,
    ExtractedDoc,
    ExtractorError,
    Section,
)
from dragndoc.log import get_logger
from dragndoc.llm import enrich, EnrichmentResult
from dragndoc.meta_store import (
    OcrInfo,
    doc_from_enrichment,
    get_by_file,
    relative_to_root,
    upsert,
    utc_now_iso,
)
from dragndoc.metadata.hashing import hash_file
from dragndoc.ocr import (
    OcrDecision,
    run_ocr,
    tesseract_available,
    tesseract_version,
)
from dragndoc.treewalk import is_in_blocked_subtree


log = get_logger(__name__)


@dataclass
class DigestResult:
    path: Path
    ocr_decision: str = "no_ocr"
    llm_tier: str = "skipped"
    category: str = "Unknown"
    metadata_target: str = "unknown"
    duration_ms: int = 0
    error: str | None = None
    enrichment: EnrichmentResult | None = None
    doc_id: int | None = None


def _file_modified_iso(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_expected_file_facts(
    path: Path,
    *,
    expected_size: int | None,
    expected_mtime: str | None,
) -> tuple[int, str | None]:
    st = path.stat()
    modified = _file_modified_iso(path)
    if expected_size is not None and st.st_size != expected_size:
        raise ValueError(f"file size changed while digesting {path}: expected {expected_size}, got {st.st_size}")
    if expected_mtime is not None and modified != expected_mtime:
        raise ValueError(f"file mtime changed while digesting {path}: expected {expected_mtime}, got {modified}")
    return st.st_size, modified


def _ocr_info_for(doc: ExtractedDoc) -> OcrInfo:
    settings = get_settings()
    if not doc.ocr_used:
        return OcrInfo(decision=doc.ocr_decision)
    return OcrInfo(
        decision=doc.ocr_decision,
        done=utc_now_iso(),
        engine="tesseract",
        engine_ver=tesseract_version(),
        langs=[s.strip() for s in settings.tesseract.langs.replace("+", ",").split(",") if s.strip()],
    )


def _maybe_run_ocr(doc: ExtractedDoc, decision: OcrDecision) -> tuple[ExtractedDoc, OcrInfo]:
    settings = get_settings()
    info = OcrInfo(decision=decision.action)
    if decision.action in {"no_ocr", "skip_encrypted"}:
        return doc, info
    if not tesseract_available():
        log.warning("OCR requested for %s but Tesseract is unavailable; skipping.", doc.path)
        return doc, OcrInfo(decision="ocr_unavailable")
    pages = decision.pages if decision.action == "ocr_pages" else None
    try:
        text = run_ocr(doc.path, langs=settings.tesseract.langs, pages=pages)
    except Exception as exc:  # noqa: BLE001
        log.error("OCR failed for %s: %s", doc.path, exc)
        return doc, OcrInfo(decision="ocr_failed")
    cfg = CapConfig.from_settings(settings)
    combined = (doc.text + "\n\n" + text).strip() if doc.text else text
    doc.sections = [Section(label=None, text=trim_to_word_boundary(combined, cfg.target_chars), index=0)]
    doc.total_sections = None
    doc.refresh_text()
    doc.ocr_used = True
    doc.ocr_decision = decision.action
    doc.ocr_pages = pages
    info = OcrInfo(
        decision=decision.action,
        done=utc_now_iso(),
        engine="tesseract",
        engine_ver=tesseract_version(),
        langs=[s.strip() for s in settings.tesseract.langs.replace("+", ",").split(",") if s.strip()],
    )
    return doc, info


def _hints_for(doc: ExtractedDoc) -> dict:
    """Build the LLM context dict: filesystem facts + the file's own embedded
    metadata (PDF DocInfo+XMP, Office core/custom props, EXIF, HTML <meta>,
    EPUB Dublin Core). Each populated by the extractor.
    """
    hints: dict = {
        "filename": doc.path.name,
        "extension": doc.path.suffix.lstrip("."),
        "format": doc.format,
        "byte_size": doc.path.stat().st_size,
    }
    if doc.extracted_metadata:
        hints.update(doc.extracted_metadata)
    return hints


def digest_file(
    path: Path,
    *,
    dry_run: bool = False,
    force_ocr: bool = False,
    file_hash: str | None = None,
    expected_size: int | None = None,
    expected_mtime: str | None = None,
) -> DigestResult:
    started = time.perf_counter()
    settings = get_settings()
    result = DigestResult(path=path)
    log.info("digesting %s%s", path, " (dry-run)" if dry_run else "")

    if not path.exists() or not path.is_file():
        result.error = "missing_or_not_file"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("cannot digest %s: %s", path, result.error)
        return result

    if is_in_blocked_subtree(path, stop_at=settings.docs):
        result.error = "blocked_by_meta_file"
        result.metadata_target = "skipped"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info("skipping %s: ancestor directory contains .meta marker file", path)
        return result

    try:
        doc = dispatch_extract(path)
    except EncryptedDocumentError as exc:
        result.error = f"encrypted: {exc}"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("encrypted document %s: %s", path, exc)
        return result
    except ExtractorError as exc:
        result.error = f"extract_failed: {exc}"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("extraction failed for %s: %s", path, exc)
        return result
    log.debug("extracted %s: format=%s text=%dchars", path, doc.format, len(doc.text or ""))

    ocr_info = _ocr_info_for(doc)
    result.ocr_decision = ocr_info.decision
    if force_ocr:
        decision = OcrDecision(action="ocr_full", reason="forced")
        log.debug("ocr decision for %s: %s (%s)", path, decision.action, decision.reason or "-")
        doc, ocr_info = _maybe_run_ocr(doc, decision)
        result.ocr_decision = ocr_info.decision

    log.debug("enriching %s (%d chars)", path, len(doc.text or ""))
    enrichment = enrich(doc, _hints_for(doc))
    result.enrichment = enrichment
    result.llm_tier = enrichment.tier
    result.category = enrichment.category

    if file_hash is None:
        file_hash = hash_file(path)
    _assert_expected_file_facts(path, expected_size=expected_size, expected_mtime=expected_mtime)

    if dry_run:
        result.metadata_target = "dry_run"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "digested %s | ocr=%s tier=%s category=%s target=dry_run | %dms",
            path, result.ocr_decision, result.llm_tier, result.category, result.duration_ms,
        )
        return result

    new_doc = doc_from_enrichment(
        path,
        enrichment=enrichment.as_dict(),
        file_hash=file_hash,
        ocr_info=ocr_info,
        summary=enrichment.summary,
    )
    existing = get_by_file(path)
    if existing is not None:
        new_doc.dup = existing.dup
    result.doc_id = upsert(new_doc)
    result.metadata_target = "db"

    from dragndoc.triage import enqueue as triage_enqueue

    try:
        triage_enqueue(result.doc_id, reason="digested")
    except Exception as exc:  # noqa: BLE001
        log.warning("triage enqueue failed for %s: %s", path, exc)

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "digested %s | ocr=%s tier=%s category=%s target=%s id=%s | %dms",
        path, result.ocr_decision, result.llm_tier, result.category,
        result.metadata_target, result.doc_id, result.duration_ms,
    )
    return result


def format_result_line(result: DigestResult) -> str:
    rel = result.path.name
    return (
        f"{rel} | ocr={result.ocr_decision} | tier={result.llm_tier} "
        f"| category={result.category} | target={result.metadata_target} "
        f"| {result.duration_ms}ms"
        + (f" | error={result.error}" if result.error else "")
    )
