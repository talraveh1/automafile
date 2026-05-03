"""Shared text quality checks for strict and tolerant decoders."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError


_BINARY_SIGNATURES = (
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
    b"PK\x03\x04",
    b"BM",
)


def looks_binary_or_garbled(raw: bytes, text: str) -> bool:
    if not raw:
        return False
    if raw.startswith(_BINARY_SIGNATURES):
        return True
    if b"\x00" in raw:
        return True

    length = max(len(text), 1)
    replacement_ratio = text.count("\ufffd") / length
    if replacement_ratio > 0.01:
        return True

    control_chars = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r")
    return control_chars / length > 0.05


def raise_if_garbled(raw: bytes, text: str, path: Path) -> None:
    if looks_binary_or_garbled(raw, text):
        raise CorruptDocumentError(f"Decoded text looks binary or garbled: {path}")
