"""End-to-end per-file digest: extract → enrich → upsert metadata row."""

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
    file_modified_iso,
    get_by_file,
    upsert,
)
from dragndoc.metadata.hashing import hash_file
from dragndoc.ocr import (
    OcrDecision,
    run_ocr,
    tesseract_available,
)
from dragndoc.treewalk import is_in_opaque_subtree


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


def _assert_expected_file_facts(
    path: Path,
    *,
    expected_size: int | None,
    expected_mtime: str | None,
) -> tuple[int, str | None]:
    # fail fast if a scan candidate changed between planning and digesting
    st = path.stat()
    modified = file_modified_iso(path)
    if expected_size is not None and st.st_size != expected_size:
        raise ValueError(f"File size changed while digesting {path}: expected {expected_size}, got {st.st_size}")
    if expected_mtime is not None and modified != expected_mtime:
        raise ValueError(f"File mtime changed while digesting {path}: expected {expected_mtime}, got {modified}")
    return st.st_size, modified


def _maybe_run_ocr(doc: ExtractedDoc, decision: OcrDecision) -> tuple[ExtractedDoc, OcrInfo]:
    settings = get_settings()
    if decision.action in {"no_ocr", "skip_encrypted"}:
        # keep the skip reason so downstream metadata reflects why OCR never ran
        return doc, OcrInfo(decision=decision.action)
    if not tesseract_available():
        log.warning("OCR requested for %s but Tesseract is unavailable; skipping.", doc.path)
        return doc, OcrInfo(decision="ocr_unavailable")
    # page-scoped OCR keeps the expensive fallback targeted when only some pages need rescue
    pages = decision.pages if decision.action == "ocr_pages" else None
    try:
        text = run_ocr(doc.path, langs=settings.tesseract.langs, pages=pages)
    except Exception as exc:  # noqa: BLE001
        log.error("OCR failed for %s: %s", doc.path, exc)
        return doc, OcrInfo(decision="ocr_failed")
    cfg = CapConfig.from_settings(settings)
    combined = (doc.text + "\n\n" + text).strip() if doc.text else text
    # collapse extractor text and OCR text into one section so enrichment sees a single body
    doc.sections = [Section(label=None, text=trim_to_word_boundary(combined, cfg.target_chars), index=0)]
    doc.total_sections = None
    doc.refresh_text()
    doc.ocr_used = True
    doc.ocr_decision = decision.action
    doc.ocr_pages = pages
    return doc, OcrInfo.for_tesseract_run(decision.action)


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


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class _DigestAbort(Exception):
    """Internal signal that an early-exit branch in ``digest_file`` fired."""

    def __init__(self, code: str, *, metadata_target: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.metadata_target = metadata_target


def _check_digestible(path: Path, settings) -> None:
    if not path.exists() or not path.is_file():
        log.error("Cannot digest %s: missing_or_not_file", path)
        raise _DigestAbort("missing_or_not_file")
    if is_in_opaque_subtree(path, stop_at=settings.docs):
        # respect directory-level opt-outs before any extraction, OCR, or hashing work starts
        log.info("Skipping %s: ancestor directory is opaque", path)
        raise _DigestAbort("blocked_opaque_subtree", metadata_target="skipped")


def _extract_or_abort(path: Path) -> ExtractedDoc:
    try:
        return dispatch_extract(path)
    except EncryptedDocumentError as exc:
        log.error("Encrypted document %s: %s", path, exc)
        raise _DigestAbort(f"encrypted: {exc}") from exc
    except ExtractorError as exc:
        log.error("Extraction failed for %s: %s", path, exc)
        raise _DigestAbort(f"extract_failed: {exc}") from exc


def _persist_and_enqueue(
    path: Path,
    *,
    enrichment: EnrichmentResult,
    file_hash: str,
    ocr_info: OcrInfo,
) -> int:
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
    doc_id = upsert(new_doc)

    from dragndoc.triage import enqueue as triage_enqueue

    try:
        # enqueue only after the row exists so triage can always resolve the doc id
        triage_enqueue(doc_id, reason="digested")
    except Exception as exc:  # noqa: BLE001
        log.warning("Triage enqueue failed for %s: %s", path, exc)
    return doc_id


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
    log.info("Digesting %s%s", path, " (dry-run)" if dry_run else "")

    try:
        _check_digestible(path, settings)
        doc = _extract_or_abort(path)
        log.debug("Extracted %s: format=%s text=%dchars", path, doc.format, len(doc.text or ""))

        ocr_info = OcrInfo.for_tesseract_run(doc.ocr_decision) if doc.ocr_used else OcrInfo(decision=doc.ocr_decision)
        result.ocr_decision = ocr_info.decision
        if force_ocr:
            # manual force bypasses the normal OCR decision path for this single digest
            decision = OcrDecision(action="ocr_full", reason="forced")
            log.debug("OCR decision for %s: %s (%s)", path, decision.action, decision.reason or "-")
            doc, ocr_info = _maybe_run_ocr(doc, decision)
            result.ocr_decision = ocr_info.decision

        log.debug("Enriching %s (%d chars)", path, len(doc.text or ""))
        enrichment = enrich(doc, _hints_for(doc))
        result.enrichment = enrichment
        result.llm_tier = enrichment.tier
        result.category = enrichment.category

        if file_hash is None:
            file_hash = hash_file(path)
        _assert_expected_file_facts(path, expected_size=expected_size, expected_mtime=expected_mtime)

        if dry_run:
            # dry runs stop after enrichment so callers can inspect the result without mutating state
            result.metadata_target = "dry_run"
        else:
            result.doc_id = _persist_and_enqueue(
                path, enrichment=enrichment, file_hash=file_hash, ocr_info=ocr_info,
            )
            result.metadata_target = "db"
    except _DigestAbort as abort:
        result.error = abort.code
        if abort.metadata_target is not None:
            result.metadata_target = abort.metadata_target
        result.duration_ms = _elapsed_ms(started)
        return result

    result.duration_ms = _elapsed_ms(started)
    log.info(
        "Digested %s | ocr=%s tier=%s category=%s target=%s id=%s | %dms",
        path, result.ocr_decision, result.llm_tier, result.category,
        result.metadata_target, result.doc_id, result.duration_ms,
    )
    return result


def format_result_line(result: DigestResult) -> str:
    # watcher and CLI share this compact status line
    rel = result.path.name
    return (
        f"{rel} | ocr={result.ocr_decision} | tier={result.llm_tier} "
        f"| category={result.category} | target={result.metadata_target} "
        f"| {result.duration_ms}ms"
        + (f" | error={result.error}" if result.error else "")
    )
