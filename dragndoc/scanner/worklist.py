"""Scan report and digest worklist shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class DigestCandidate:
    """One filesystem row that should be passed to ``digest_file``."""

    rel: str
    size: int
    mtime: str | None
    file_hash: str
    reason: str
    doc_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.rel,
            "rel": self.rel,
            "size": self.size,
            "mtime": self.mtime,
            "file_hash": self.file_hash,
            "hash": self.file_hash,
            "reason": self.reason,
            "doc_id": self.doc_id,
            **self.details,
        }


@dataclass
class UnprocessableEntry:
    """A file the scanner saw but cannot safely digest."""

    rel: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"relative_path": self.rel, "reason": self.reason}


@dataclass
class WorklistForDigest:
    """Grouped digest candidates, preserving the reason each file was selected."""

    new_files: list[DigestCandidate] = field(default_factory=list)
    changed_files: list[DigestCandidate] = field(default_factory=list)
    partial_metadata: list[DigestCandidate] = field(default_factory=list)
    stale_metadata: list[DigestCandidate] = field(default_factory=list)
    ocr_review: list[DigestCandidate] = field(default_factory=list)
    asr_review: list[DigestCandidate] = field(default_factory=list)
    unprocessable: list[UnprocessableEntry] = field(default_factory=list)

    def iter_digest_candidates(self) -> Iterator[DigestCandidate]:
        seen: set[str] = set()
        # yield each path once even if it appears in multiple worklist buckets
        for bucket in (
            self.new_files,
            self.changed_files,
            self.partial_metadata,
            self.stale_metadata,
            self.ocr_review,
            self.asr_review,
        ):
            for candidate in bucket:
                if candidate.rel in seen:
                    continue
                seen.add(candidate.rel)
                yield candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "new_files": [item.to_dict() for item in self.new_files],
            "changed_files": [item.to_dict() for item in self.changed_files],
            "partial_metadata": [item.to_dict() for item in self.partial_metadata],
            "stale_metadata": [item.to_dict() for item in self.stale_metadata],
            "ocr_review": [item.to_dict() for item in self.ocr_review],
            "asr_review": [item.to_dict() for item in self.asr_review],
            "unprocessable": [item.to_dict() for item in self.unprocessable],
        }


@dataclass
class MergeRecord:
    old_path: str
    new_path: str
    winner_id: int
    loser_id: int
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "winner_id": self.winner_id,
            "loser_id": self.loser_id,
            "hash": self.hash,
        }


@dataclass
class OrphanInfo:
    doc_id: int
    recorded_path: str
    hash: str
    size: int
    reason: str = "no_hash_match"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "relative_path": self.recorded_path,
            "recorded_path": self.recorded_path,
            "hash": self.hash,
            "size": self.size,
            "reason": self.reason,
        }


@dataclass
class ReconciliationReport:
    renames: list[tuple[str, str]] = field(default_factory=list)
    merges: list[MergeRecord] = field(default_factory=list)
    unresolved_orphans: list[OrphanInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "renames": [{"old_path": old, "new_path": new} for old, new in self.renames],
            "merges": [item.to_dict() for item in self.merges],
            "unresolved_orphans": [item.to_dict() for item in self.unresolved_orphans],
        }


@dataclass
class ScanReport:
    """Public scan result shape used by CLI, tests, and JSON output."""

    ran_at: str
    docs_root: str
    files_seen: int
    skipped: int
    worklist: WorklistForDigest
    reconciliation: ReconciliationReport
    tree_size: int = 0

    @property
    def docs(self) -> str:
        return self.docs_root

    @property
    def files_needing_metadata(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.new_files]

    @property
    def files_needing_ocr(self) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.worklist.new_files
            if item.details.get("needs_ocr")
        ]

    @property
    def files_needing_asr(self) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.worklist.new_files
            if item.details.get("needs_asr")
        ]

    @property
    def files_with_partial_metadata(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.partial_metadata]

    @property
    def files_with_stale_metadata(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.stale_metadata]

    @property
    def ocr_review_candidates(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.ocr_review]

    @property
    def asr_review_candidates(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.asr_review]

    @property
    def missing_files(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.reconciliation.unresolved_orphans]

    @property
    def unprocessable_files(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.worklist.unprocessable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "docs_root": self.docs_root,
            "docs": self.docs_root,
            "tree_size": self.tree_size,
            "files_seen": self.files_seen,
            "skipped": self.skipped,
            "worklist": self.worklist.to_dict(),
            "reconciliation": self.reconciliation.to_dict(),
            "files_needing_metadata": self.files_needing_metadata,
            "files_needing_ocr": self.files_needing_ocr,
            "files_needing_asr": self.files_needing_asr,
            "files_with_partial_metadata": self.files_with_partial_metadata,
            "files_with_stale_metadata": self.files_with_stale_metadata,
            "ocr_review_candidates": self.ocr_review_candidates,
            "asr_review_candidates": self.asr_review_candidates,
            "missing_files": self.missing_files,
            "unprocessable_files": self.unprocessable_files,
        }
