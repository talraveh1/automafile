"""End-to-end per-file processing: extract → OCR → enrich → write metadata."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from automafile.config import get_settings
from automafile.dispatch import extract as dispatch_extract
from automafile.extractors.base import (
    EncryptedDocumentError,
    ExtractedDoc,
    ExtractorError,
)
from automafile.log import get_logger
from automafile.llm import enrich, EnrichmentResult
from automafile.metadata import native as native_meta
from automafile.metadata import sidecar
from automafile.metadata.hashing import hash_file
from automafile.metadata.schema import MetadataDoc, OcrBlock, utc_now_iso
from automafile.ocr import (
    OcrDecision,
    pdf_ocr_decision,
    record_ocr_metadata,
    run_ocr,
    tesseract_available,
    tesseract_version,
)


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


def _decide_ocr(doc: ExtractedDoc) -> OcrDecision:
    if doc.format == "image":
        return OcrDecision(action="ocr_full", reason="image_format")
    if doc.format == "pdf":
        return pdf_ocr_decision(doc.path, per_page_chars=doc.per_page_chars)
    return OcrDecision(action="no_ocr")


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
    doc.text = (doc.text + "\n\n" + text).strip() if doc.text else text
    doc.ocr_used = True
    block = OcrBlock(
        decision=decision.action,
        done_at=utc_now_iso(),
        engine="tesseract",
        engine_version=tesseract_version(),
        languages=settings.tesseract_langs,
    )
    return doc, block


def _hints_for(doc: ExtractedDoc) -> dict:
    return {
        "filename": doc.path.name,
        "extension": doc.path.suffix.lstrip("."),
        "format": doc.format,
        "byte_size": doc.path.stat().st_size,
        "has_native_metadata": bool(doc.native_metadata),
    }


def process_file(path: Path, *, dry_run: bool = False, force_ocr: bool = False) -> ProcessResult:
    started = time.perf_counter()
    result = ProcessResult(path=path)
    log.info("processing %s%s", path, " (dry-run)" if dry_run else "")

    if not path.exists() or not path.is_file():
        result.error = "missing_or_not_file"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("cannot process %s: %s", path, result.error)
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

    decision = _decide_ocr(doc)
    if force_ocr and decision.action == "no_ocr":
        decision = OcrDecision(action="ocr_full", reason="forced")
    result.ocr_decision = decision.action
    log.debug("ocr decision for %s: %s (%s)", path, decision.action, decision.reason or "-")
    doc, ocr_block = _maybe_run_ocr(doc, decision)

    log.debug("enriching %s (%d chars)", path, len(doc.text or ""))
    enrichment = enrich(doc.text, _hints_for(doc))
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

    wrote_native = False
    if doc.supports_native_metadata and native_meta.supports(path):
        try:
            native_meta.write(path, enrichment_dict)
            wrote_native = True
            result.metadata_target = "native"
        except native_meta.NativeMetadataError as exc:
            log.warning("Native metadata write failed for %s: %s", path, exc)

    meta_doc = sidecar.build_meta_doc_for_new_file(
        path,
        enrichment_dict,
        ocr_block=ocr_block.model_dump(),
    )
    if not wrote_native:
        spath = sidecar.write(path, meta_doc, summary_body=enrichment.summary)
        result.sidecar_path = spath
        result.metadata_target = "sidecar"
    else:
        # also drop a sidecar so the in-tree memory is consistent
        spath = sidecar.write(path, meta_doc, summary_body=enrichment.summary)
        result.sidecar_path = spath
        result.metadata_target = "native+sidecar"

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
