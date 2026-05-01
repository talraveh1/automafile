"""Ollama client with tiered JSON parse fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from automafile.config import get_settings
from automafile.log import get_logger


log = get_logger(__name__)


# Hebrew Unicode block U+0590..U+05FF — applied character-class style
HEBREW_RANGE = "֐-׿"
HEBREW_QUOTE_RE = re.compile(
    rf'(?<=[{HEBREW_RANGE}])"|"(?=[{HEBREW_RANGE}])'
)
HEBREW_GERSHAYIM = "״"

# ASCII printable range used by sanitizer
ASCII_QUOTE = '"'

_KNOWN_KEYS = ("title", "summary", "reason", "correspondent", "subcategory")
# match ``"key":"value"`` where value ends at the closing quote that's followed by
# either `,` + next-known-key, or `,` + `<line-break>` + next-known-key, or end of object.
STRING_KEY_PATTERN = re.compile(
    r'"(' + "|".join(_KNOWN_KEYS) + r')"\s*:\s*"(.*?)"(?=\s*[,}])',
    re.DOTALL,
)


@dataclass
class EnrichmentResult:
    """Output of a single LLM enrichment call."""

    title: str | None = None
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "Unknown"
    subcategory: str | None = None
    correspondent: str | None = None
    date: str | None = None
    amount: float | None = None
    currency: str | None = None
    language: str = "unknown"
    confidence: str = "low"
    needs_review: bool = True
    reason: str | None = None
    tier: str = "unknown"
    raw_response: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "category": self.category,
            "subcategory": self.subcategory,
            "correspondent": self.correspondent,
            "date": self.date,
            "amount": self.amount,
            "currency": self.currency,
            "language": self.language,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "reason": self.reason,
        }


def sanitize_excerpt(text: str) -> str:
    """Replace double-quotes adjacent to Hebrew characters with gershayim."""
    if not text:
        return text
    return HEBREW_QUOTE_RE.sub(HEBREW_GERSHAYIM, text)


def _category_alias(category: str) -> str:
    """Map raw model output to user's canonical taxonomy keys."""
    if not category:
        return "Unknown"
    key = category.strip().lower()
    aliases = {
        "medical": "Personal",
        "health": "Personal",
        "healthcare": "Personal",
        "finance": "Financial",
        "financial": "Financial",
        "legal": "Legal",
        "law": "Legal",
        "research": "Research",
        "academic": "Research",
        "teaching": "Teaching",
        "education": "Teaching",
        "personal": "Personal",
        "family": "Personal",
        "media": "Media",
        "photo": "Media",
        "photos": "Media",
        "unknown": "Unknown",
        "other": "Unknown",
    }
    return aliases.get(key, category.strip())


def _build_prompt(text: str, hints: dict, taxonomy: list[str]) -> str:
    template_path = Path(__file__).parent / "prompts" / "triage.txt"
    template = template_path.read_text(encoding="utf-8")
    safe_text = sanitize_excerpt(text or "")[:6000]
    hints_text = "\n".join(f"- {k}: {v}" for k, v in (hints or {}).items()) or "(none)"
    taxonomy_text = ", ".join(taxonomy) if taxonomy else "Financial, Legal, Research, Teaching, Personal, Media, Unknown"
    return (
        template.replace("{taxonomy}", taxonomy_text)
        .replace("{hints}", hints_text)
        .replace("{text}", safe_text)
    )


def _ollama_generate(prompt: str, *, extra_options: dict | None = None) -> str:
    settings = get_settings()
    url = settings.ollama_url.rstrip("/") + "/api/generate"
    options = {
        "temperature": 0.1,
        "num_ctx": 8192,
        "num_predict": 1024,
    }
    if extra_options:
        options.update(extra_options)
    body = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": options,
    }
    resp = requests.post(url, json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", "")


def _strict_parse(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _repair_parse(raw: str) -> dict | None:
    """Tier 2: escape stray double-quotes inside known string-valued keys."""
    repaired = STRING_KEY_PATTERN.sub(
        lambda m: '"{key}":"{val}"'.format(
            key=m.group(1),
            val=m.group(2).replace('"', '\\"'),
        ),
        raw,
    )
    try:
        return json.loads(repaired)
    except Exception:
        return None


_FIELD_REGEXES: dict[str, re.Pattern[str]] = {
    "title": re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    "summary": re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    "category": re.compile(r'"category"\s*:\s*"([^"]+)"'),
    "subcategory": re.compile(r'"subcategory"\s*:\s*"([^"]+)"'),
    "correspondent": re.compile(r'"correspondent"\s*:\s*"([^"]+)"'),
    "language": re.compile(r'"language"\s*:\s*"([^"]+)"'),
    "confidence": re.compile(r'"confidence"\s*:\s*"([^"]+)"'),
    "reason": re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"'),
}
_TAGS_RE = re.compile(r'"tags"\s*:\s*\[([^\]]*)\]')
_DATE_RE = re.compile(r'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
_AMOUNT_RE = re.compile(r'"amount"\s*:\s*([0-9]+(?:\.[0-9]+)?)')
_CURRENCY_RE = re.compile(r'"currency"\s*:\s*"([A-Z]{3})"')
_NEEDS_REVIEW_RE = re.compile(r'"needs_review"\s*:\s*(true|false)')


def _regex_recover(raw: str) -> dict | None:
    out: dict[str, Any] = {}
    for key, pat in _FIELD_REGEXES.items():
        m = pat.search(raw)
        if m:
            out[key] = m.group(1)
    tags_match = _TAGS_RE.search(raw)
    if tags_match:
        items = re.findall(r'"([^"]+)"', tags_match.group(1))
        out["tags"] = items
    date_match = _DATE_RE.search(raw)
    if date_match:
        out["date"] = date_match.group(1)
    amount_match = _AMOUNT_RE.search(raw)
    if amount_match:
        try:
            out["amount"] = float(amount_match.group(1))
        except ValueError:
            pass
    currency_match = _CURRENCY_RE.search(raw)
    if currency_match:
        out["currency"] = currency_match.group(1)
    nr_match = _NEEDS_REVIEW_RE.search(raw)
    if nr_match:
        out["needs_review"] = nr_match.group(1) == "true"
    return out or None


def _coerce_to_result(parsed: dict, tier: str, raw: str) -> EnrichmentResult:
    if not isinstance(parsed, dict):
        parsed = {}
    res = EnrichmentResult(
        title=parsed.get("title") or None,
        summary=str(parsed.get("summary") or ""),
        tags=[str(t) for t in (parsed.get("tags") or []) if t],
        category=_category_alias(str(parsed.get("category") or "Unknown")),
        subcategory=parsed.get("subcategory") or None,
        correspondent=parsed.get("correspondent") or None,
        date=parsed.get("date") or None,
        amount=(float(parsed["amount"]) if parsed.get("amount") not in (None, "", "null") else None),
        currency=parsed.get("currency") or None,
        language=str(parsed.get("language") or "unknown"),
        confidence=str(parsed.get("confidence") or "low"),
        needs_review=bool(parsed.get("needs_review", True)),
        reason=parsed.get("reason") or None,
        tier=tier,
        raw_response=raw,
    )
    if not res.summary:
        res.needs_review = True
    return res


def _placeholder_result(raw: str) -> EnrichmentResult:
    return EnrichmentResult(
        title=None,
        summary="",
        tags=[],
        category="Unknown",
        subcategory=None,
        correspondent=None,
        date=None,
        amount=None,
        currency=None,
        language="unknown",
        confidence="low",
        needs_review=True,
        reason="LLM output could not be parsed.",
        tier="placeholder",
        raw_response=raw,
    )


def parse_with_tiers(raw: str, prompt_for_retry: str | None = None) -> EnrichmentResult:
    """Pure parsing pipeline. Used by tests and by ``enrich``."""
    parsed = _strict_parse(raw)
    if parsed is not None:
        return _coerce_to_result(parsed, "strict", raw)

    parsed = _repair_parse(raw)
    if parsed is not None:
        return _coerce_to_result(parsed, "repair", raw)

    if prompt_for_retry:
        retry_prompt = prompt_for_retry + "\n\nReminder: do NOT use double-quote characters inside string values."
        try:
            retry_raw = _ollama_generate(retry_prompt)
            parsed = _strict_parse(retry_raw) or _repair_parse(retry_raw)
            if parsed is not None:
                return _coerce_to_result(parsed, "retry", retry_raw)
            partial = _regex_recover(retry_raw)
            if partial:
                return _coerce_to_result(partial, "regex", retry_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM retry failed: %s", exc)

    partial = _regex_recover(raw)
    if partial:
        return _coerce_to_result(partial, "regex", raw)

    return _placeholder_result(raw)


def enrich(text: str, hints: dict | None = None, taxonomy: list[str] | None = None) -> EnrichmentResult:
    """Send text to Ollama, parse with tiered fallback, return enrichment."""
    taxonomy = taxonomy or ["Financial", "Legal", "Research", "Teaching", "Personal", "Media", "Unknown"]
    prompt = _build_prompt(text, hints or {}, taxonomy)
    try:
        raw = _ollama_generate(prompt)
    except Exception as exc:  # noqa: BLE001
        log.error("Ollama call failed: %s", exc)
        return _placeholder_result(f"<<error>> {exc}")
    return parse_with_tiers(raw, prompt_for_retry=prompt)


def ollama_available() -> bool:
    settings = get_settings()
    try:
        resp = requests.get(settings.ollama_url.rstrip("/") + "/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def ollama_has_model() -> bool:
    settings = get_settings()
    try:
        resp = requests.get(settings.ollama_url.rstrip("/") + "/api/tags", timeout=5)
        if resp.status_code != 200:
            return False
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(m.startswith(settings.ollama_model.split(":", 1)[0]) for m in models)
    except Exception:
        return False
