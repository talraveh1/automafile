"""XLSX extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


def extract(path: Path) -> ExtractedDoc:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise CorruptDocumentError("openpyxl is not installed") from exc
    try:
        wb = load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"openpyxl failed for {path}: {exc}") from exc

    chunks: list[str] = []
    for ws in wb.worksheets:
        chunks.append(f"# Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            line = "\t".join("" if c is None else str(c) for c in row)
            if line.strip():
                chunks.append(line)

    metadata: dict = {}
    try:
        cp = wb.properties
        for attr in ("title", "subject", "keywords", "category", "description", "creator"):
            v = getattr(cp, attr, None)
            if v:
                metadata[attr] = v
    except Exception:
        pass

    wb.close()

    return ExtractedDoc(
        path=path,
        text="\n".join(chunks),
        native_metadata=metadata,
        format="xlsx",
        supports_native_metadata=True,
    )
