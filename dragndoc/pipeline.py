"""End-to-end per-file processing: extract → enrich → write metadata."""

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
from dragndoc.metadata import sidecar
from dragndoc.metadata.schema import OcrBlock, utc_now_iso
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
    sidecar_path: Path | None = None


def _ocr_block_for(doc: ExtractedDoc) -> OcrBlock:
    settings = get_settings()
    if not doc.ocr_used:
        return OcrBlock(decision=doc.ocr_decision)
    return OcrBlock(
        decision=doc.ocr_decision,
        done_at=utc_now_iso(),
        engine="tesseract",
        engine_version=tesseract_version(),
        languages=settings.tesseract_langs,
    )


def _maybe_run_ocr(doc: ExtractedDoc, decision: OcrDecision) -> tuple[ExtractedDoc, OcrBlock]:
    settings = get_settings()
    block = OcrBlock(decision=decision.action)
    if decision.action in {"no_ocr", "skip_encrypted"}:
        return doc, block
    if not tesseract_available():
        log.warning("OCR requested for %s but Tesseract is unavailable; skipping.", doc.path)
        block = OcrBlock(decision="ocr_unavailable")
        return doc, block
    pages = decision.pages if decision.action == "ocr_pages" else None
    try:
        text = run_ocr(doc.path, langs=settings.tesseract_langs, pages=pages)
    except Exception as exc:  # noqa: BLE001
        log.error("OCR failed for %s: %s", doc.path, exc)
        block = OcrBlock(decision="ocr_failed")
        return doc, block
    cfg = CapConfig.from_settings(settings)
    combined = (doc.text + "\n\n" + text).strip() if doc.text else text
    doc.sections = [Section(label=None, text=trim_to_word_boundary(combined, cfg.target_chars), index=0)]
    doc.total_sections = None
    doc.refresh_text()
    doc.ocr_used = True
    doc.ocr_decision = decision.action
    doc.ocr_pages = pages
    block = OcrBlock(
        decision=decision.action,
        done_at=utc_now_iso(),
        engine="tesseract",
        engine_version=tesseract_version(),
        languages=settings.tesseract_langs,
    )
    return doc, block


def _hints_for(doc: ExtractedDoc) -> dict:
    """Build the LLM context dict: filesystem facts + the file's own embedded
    metadata (PDF DocInfo+XMP, Office core/custom props, EXIF, HTML <meta>,
    EPUB Dublin Core). Each populated by the extractor, already cleaned and
    length-clipped via ``extractors._meta.collect``.
    """
    hints: dict = {
        "filename": doc.path.name,
        "extension": doc.path.suffix.lstrip("."),
        "format": doc.format,
        "byte_size": doc.path.stat().st_size,
    }
    if doc.extracted_metadata:
        # extractor keys (e.g. ``core_title``, ``exif_DateTimeOriginal``) are
        # namespaced enough to not collide with the four filesystem keys above.
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
        log.info("skipping %s: ancestor directory contains file %s", path, settings.meta_subfolder)
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

    ocr_block = _ocr_block_for(doc)
    result.ocr_decision = ocr_block.decision
    if force_ocr:
        decision = OcrDecision(action="ocr_full", reason="forced")
        log.debug("ocr decision for %s: %s (%s)", path, decision.action, decision.reason or "-")
        doc, ocr_block = _maybe_run_ocr(doc, decision)
        result.ocr_decision = ocr_block.decision

    log.debug("enriching %s (%d chars)", path, len(doc.text or ""))
    enrichment = enrich(doc, _hints_for(doc))
    result.enrichment = enrichment
    result.llm_tier = enrichment.tier
    result.category = enrichment.category

    enrichment_dict = enrichment.as_dict()

    if dry_run:
        result.metadata_target = "dry_run"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "processed %s | ocr=%s tier=%s category=%s target=dry_run | %dms",
            path, result.ocr_decision, result.llm_tier, result.category, result.duration_ms,
        )
        return result

    meta_doc = sidecar.build_meta_doc_for_new_file(
        path,
        enrichment_dict,
        ocr_block=ocr_block.model_dump(),
    )
    spath = sidecar.write(path, meta_doc, summary_body=enrichment.summary)
    result.sidecar_path = spath
    result.metadata_target = "sidecar"

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "processed %s | ocr=%s tier=%s category=%s target=%s | %dms",
        path, result.ocr_decision, result.llm_tier, result.category,
        result.metadata_target, result.duration_ms,
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
