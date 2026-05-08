"""Filesystem-vs-DB scanner and reconciler."""

from __future__ import annotations

from dragndoc.ocr import tesseract_version
from dragndoc.scanner.passes import SUPPORTED_EXT, run_scan
from dragndoc.scanner.reconcile import (
    ContentChanged,
    NewFile,
    NoChange,
    ReconcileOutcome,
    Renamed,
    reconcile_single,
)
from dragndoc.scanner.worklist import (
    DigestCandidate,
    MergeRecord,
    OrphanInfo,
    ReconciliationReport,
    ScanReport,
    UnprocessableEntry,
    WorklistForDigest,
)


__all__ = [
    "SUPPORTED_EXT",
    "ContentChanged",
    "DigestCandidate",
    "MergeRecord",
    "NewFile",
    "NoChange",
    "OrphanInfo",
    "ReconcileOutcome",
    "ReconciliationReport",
    "Renamed",
    "ScanReport",
    "UnprocessableEntry",
    "WorklistForDigest",
    "reconcile_single",
    "run_scan",
    "tesseract_version",
]
