"""End-to-end per-file processing: extract → enrich → upsert metadata row."""

from __future__ import annotations

import time
from dataclasses import dataclass
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
class ProcessResult:
    path: Path
    ocr_decision: str = "no_ocr"
    llm_tier: str = "skipped"
    category: str = "Unknown"
    metadata_target: str = "unknown"
    duration_ms: int = 0
    error: str | None = None
    enrichment: EnrichmentResult | None = None
    doc_id: int | None = None


def _ocr_info_for(doc: ExtractedDoc) -> OcrInfo:
    settings = get_settings()
    if not doc.ocr_used:
        return OcrInfo(decision=doc.ocr_decision)
    return OcrInfo(
        decision=doc.ocr_decision,
        done=utc_now_iso(),
        engine="tesseract",
        engine_ver=tesseract_version(),
        langs=[s.strip() for s in settings.tesseract_langs.replace("+", ",").split(",") if s.strip()],
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
        text = run_ocr(doc.path, langs=settings.tesseract_langs, pages=pages)
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
        langs=[s.strip() for s in settings.tesseract_langs.replace("+", ",").split(",") if s.strip()],
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


def process_file(path: Path, *, dry_run: bool = False, force_ocr: bool = False) -> ProcessResult:
    started = time.perf_counter()
    settings = get_settings()
    result = ProcessResult(path=path)
    log.info("processing %s%s", path, " (dry-run)" if dry_run else "")

    if not path.exists() or not path.is_file():
        result.error = "missing_or_not_file"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("cannot process %s: %s", path, result.error)
        return result

    if is_in_blocked_subtree(path, stop_at=settings.documents_root):
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

    if dry_run:
        result.metadata_target = "dry_run"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "processed %s | ocr=%s tier=%s category=%s target=dry_run | %dms",
            path, result.ocr_decision, result.llm_tier, result.category, result.duration_ms,
        )
        return result

    file_hash = hash_file(path)
    new_doc = doc_from_enrichment(
        path,
        enrichment=enrichment.as_dict(),
        file_hash=file_hash,
        ocr_info=ocr_info,
        summary=enrichment.summary,
    )
    result.doc_id = upsert(new_doc)
    result.metadata_target = "db"

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "processed %s | ocr=%s tier=%s category=%s target=%s id=%s | %dms",
        path, result.ocr_decision, result.llm_tier, result.category,
        result.metadata_target, result.doc_id, result.duration_ms,
    )
    return result


def format_result_line(result: ProcessResult) -> str:
    rel = result.path.name
    return (
        f"{rel} | ocr={result.ocr_decision} | tier={result.llm_tier} "
        f"| category={result.category} | target={result.metadata_target} "
        f"| {result.duration_ms}ms"
        + (f" | error={result.error}" if result.error else "")
    )
