"""Legacy import shims for the pre-DB metadata layer.

Prefer importing from :mod:`dragndoc.meta_store` directly. This module exists
so older call sites that did ``from dragndoc.metadata.schema import OcrBlock,
utc_now_iso`` keep working without churn.
"""

from __future__ import annotations

from dragndoc.meta_store import OcrInfo as OcrBlock, utc_now_iso


__all__ = ["OcrBlock", "utc_now_iso"]
