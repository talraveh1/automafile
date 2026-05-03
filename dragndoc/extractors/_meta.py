"""Helpers for collecting embedded metadata from documents.

Each extractor calls into here to filter and clean a flat ``{field: value}``
dict of fields the file *itself* declares (PDF DocInfo + XMP, OOXML core
properties, EXIF, HTML ``<meta>``, etc.). The cleaned dict ends up on
``ExtractedDoc.extracted_metadata`` and is rendered into the LLM prompt
as soft context — see ``dragndoc.pipeline._hints_for``.

We don't try to harmonize names across formats; each format's natural
keys (``dc:title``, ``core_creator``, ``exif_Artist``, ``meta_og:description``,
...) are kept verbatim. The LLM gets the format hint separately and sorts
it out.
"""

from __future__ import annotations

from typing import Any


_VALUE_MAX_LEN = 200
_LIST_MAX_ITEMS = 20

# keys we never want to surface — typically because the value is an opaque
# binary blob or an entire embedded payload (XMP packets, ICC profiles).
_SKIP_KEYS = {
    "icc_profile",
    "exif",  # the raw EXIF binary blob in PIL info; we extract it separately
    "photoshop",
    "XML:com.adobe.xmp",  # XMP packet inside PIL info — we read XMP via pikepdf for PDFs
    "dpi",  # noise; we don't need it for classification
    "jfif",
    "jfif_version",
    "jfif_density",
    "jfif_unit",
}

# value prefixes that signal the value is an XML/XMP packet — also skipped
_PACKET_PREFIXES = ("<?xpacket", "<x:xmpmeta", "<rdf:RDF")


def clean_value(v: Any) -> Any | None:
    """Return an LLM-friendly version of ``v``, or ``None`` to drop it.

    - Empties (None, "", [], {}) → None.
    - Bytes → utf-8 best-effort, then string rules.
    - Strings → trimmed, binary/XMP-packet-rejected, length-clipped to ``_VALUE_MAX_LEN``.
    - Lists → recursively cleaned, length-clipped to ``_LIST_MAX_ITEMS``.
    - Numbers/booleans → kept as-is.
    - Dicts → ``None`` (caller should flatten before passing).
    - Anything else → ``str(v)`` then string rules.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if "\x00" in s:
            return None
        if any(s.startswith(p) for p in _PACKET_PREFIXES):
            return None
        if len(s) > _VALUE_MAX_LEN:
            s = s[:_VALUE_MAX_LEN] + "…"
        return s
    if isinstance(v, (list, tuple)):
        cleaned: list[Any] = []
        for item in v:
            c = clean_value(item)
            if c is not None:
                cleaned.append(c)
        if not cleaned:
            return None
        if len(cleaned) > _LIST_MAX_ITEMS:
            cleaned = cleaned[:_LIST_MAX_ITEMS] + ["…"]
        return cleaned
    if isinstance(v, dict):
        return None
    s = str(v).strip()
    if not s or "\x00" in s:
        return None
    if len(s) > _VALUE_MAX_LEN:
        s = s[:_VALUE_MAX_LEN] + "…"
    return s


def collect(items: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    """Filter and clean a flat metadata dict; optionally prefix keys.

    Drops keys in ``_SKIP_KEYS`` and any value ``clean_value`` rejects.
    """
    out: dict[str, Any] = {}
    for k, v in items.items():
        if k in _SKIP_KEYS:
            continue
        cleaned = clean_value(v)
        if cleaned is None:
            continue
        key = f"{prefix}{k}" if prefix else k
        out[key] = cleaned
    return out
