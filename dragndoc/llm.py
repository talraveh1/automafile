"""Ollama client with tiered JSON parse fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from dragndoc.config import get_settings
from dragndoc.extractors.base import ExtractedDoc, Section
from dragndoc.log import get_logger


log = get_logger(__name__)


# hebrew unicode block U+0590..U+05FF, applied character-class style
HEBREW_RANGE = "֐-׿"
HEBREW_QUOTE_RE = re.compile(
    rf'(?<=[{HEBREW_RANGE}])"|"(?=[{HEBREW_RANGE}])'
)
HEBREW_GERSHAYIM = "״"

# ascii printable quote used by sanitizer
ASCII_QUOTE = '"'

_TAXONOMY_PATH_PARTS = ("memory", "taxonomy.md")
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
    review: bool = True
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
            "review": self.review,
            "reason": self.reason,
        }


def sanitize_excerpt(text: str) -> str:
    """Replace double-quotes adjacent to Hebrew characters with gershayim."""
    if not text:
        return text
    # reduce quote-heavy Hebrew fragments that commonly confuse JSON output
    return HEBREW_QUOTE_RE.sub(HEBREW_GERSHAYIM, text)


def _load_taxonomy() -> str:
    """Read the user's taxonomy markdown verbatim. Bootstrap is a prerequisite."""
    settings = get_settings()
    path = settings.repo_root.joinpath(*_TAXONOMY_PATH_PARTS)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"taxonomy file missing at {path}; run `dnd bootstrap` first"
        ) from exc


def _section_header(section: Section, total_sections: int | None) -> str:
    label = section.label or f"Section {section.index + 1}"
    if total_sections is None:
        return f"--- {label} ---"
    # keep native page/slide/chapter labels while still exposing total context
    if label.startswith(("Page ", "Slide ", "Chapter ")):
        return f"--- {label} of {total_sections} ---"
    return f"--- {label} ({section.index + 1} of {total_sections}) ---"


def _render_sections(sections: list[Section], total_sections: int | None) -> str:
    if not sections:
        return ""
    if total_sections is None and len(sections) == 1 and sections[0].label is None:
        return sections[0].text

    # deterministic section banners make partial documents legible to the LLM
    blocks = [
        f"{_section_header(section, total_sections)}\n{section.text}"
        for section in sections
    ]
    if total_sections is not None and len(sections) < total_sections:
        first = sections[0].index + 1
        last = sections[-1].index + 1
        blocks.append(f"--- (showing pages {first}-{last} of {total_sections}) ---")
    return "\n\n".join(blocks)


def _build_prompt(doc: ExtractedDoc, hints: dict, taxonomy: str) -> str:
    template_path = Path(__file__).parent / "prompts" / "triage.txt"
    template = template_path.read_text(encoding="utf-8")
    safe_text = sanitize_excerpt(_render_sections(doc.sections, doc.total_sections))
    # render embedded metadata as soft hints instead of hard classification rules
    hints_text = "\n".join(f"- {k}: {v}" for k, v in (hints or {}).items()) or "(none)"
    return (
        template.replace("{taxonomy}", taxonomy)
        .replace("{hints}", hints_text)
        .replace("{text}", safe_text)
    )


def _ollama_generate(prompt: str, *, extra_options: dict | None = None) -> str:
    import time as _time
    settings = get_settings()
    url = settings.ollama.url.rstrip("/") + "/api/generate"
    options = {
        "temperature": 0.1,
        "num_ctx": 8192,
        "num_predict": 1024,
    }
    if extra_options:
        options.update(extra_options)
    body = {
        "model": settings.ollama.model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": options,
    }
    log.debug("ollama POST %s model=%s prompt=%dchars", url, settings.ollama.model, len(prompt))
    started = _time.perf_counter()
    # let HTTP and JSON failures bubble so the caller can return a placeholder result
    resp = requests.post(url, json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("response", "")
    log.debug("ollama response: %d chars in %dms", len(raw), int((_time.perf_counter() - started) * 1000))
    return raw


def _strict_parse(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _repair_parse(raw: str) -> dict | None:
    """Tier 2: escape stray double-quotes inside known string-valued keys."""
    # known keys are repaired surgically so unrelated malformed JSON still fails this tier
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
_review_RE = re.compile(r'"review"\s*:\s*(true|false)')


def _regex_recover(raw: str) -> dict | None:
    out: dict[str, Any] = {}
    # recover fields independently so a broken field does not discard the whole response
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
    nr_match = _review_RE.search(raw)
    if nr_match:
        out["review"] = nr_match.group(1) == "true"
    return out or None


def _coerce_str_field(value: Any) -> str | None:
    """Normalize an LLM string-valued field — flattens list outputs (e.g. multi-author)."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, list):
        joined = ", ".join(str(v) for v in value if v not in (None, ""))
        return joined or None
    return str(value)


def _coerce_to_result(parsed: dict, tier: str, raw: str) -> EnrichmentResult:
    if not isinstance(parsed, dict):
        parsed = {}
    # normalize model variance into the stricter EnrichmentResult shape used downstream
    res = EnrichmentResult(
        title=_coerce_str_field(parsed.get("title")),
        summary=str(parsed.get("summary") or ""),
        tags=[str(t) for t in (parsed.get("tags") or []) if t],
        category=str(parsed.get("category") or "Unknown").strip() or "Unknown",
        subcategory=_coerce_str_field(parsed.get("subcategory")),
        correspondent=_coerce_str_field(parsed.get("correspondent")),
        date=parsed.get("date") or None,
        amount=(float(parsed["amount"]) if parsed.get("amount") not in (None, "", "null") else None),
        currency=parsed.get("currency") or None,
        language=str(parsed.get("language") or "unknown"),
        confidence=str(parsed.get("confidence") or "low"),
        review=bool(parsed.get("review", True)),
        reason=parsed.get("reason") or None,
        tier=tier,
        raw_response=raw,
    )
    if not res.summary:
        res.review = True
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
        review=True,
        reason="LLM output could not be parsed.",
        tier="placeholder",
        raw_response=raw,
    )


def parse_with_tiers(raw: str, prompt_for_retry: str | None = None) -> EnrichmentResult:
    """Pure parsing pipeline. Used by tests and by ``enrich``."""
    # try lossless JSON first, then progressively more tolerant recovery paths
    parsed = _strict_parse(raw)
    if parsed is not None:
        log.debug("llm parse tier=strict")
        return _coerce_to_result(parsed, "strict", raw)

    parsed = _repair_parse(raw)
    if parsed is not None:
        log.debug("llm parse tier=repair")
        return _coerce_to_result(parsed, "repair", raw)

    if prompt_for_retry:
        log.info("llm strict+repair failed; retrying with reminder")
        retry_prompt = prompt_for_retry + "\n\nReminder: do NOT use double-quote characters inside string values."
        try:
            retry_raw = _ollama_generate(retry_prompt)
            parsed = _strict_parse(retry_raw) or _repair_parse(retry_raw)
            if parsed is not None:
                log.debug("llm parse tier=retry")
                return _coerce_to_result(parsed, "retry", retry_raw)
            # retry output can still be useful even when it is not valid JSON
            partial = _regex_recover(retry_raw)
            if partial:
                log.debug("llm parse tier=regex (after retry)")
                return _coerce_to_result(partial, "regex", retry_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM retry failed: %s", exc)

    partial = _regex_recover(raw)
    if partial:
        log.warning("llm parse tier=regex (degraded recovery, raw=%d chars)", len(raw))
        return _coerce_to_result(partial, "regex", raw)

    log.error("llm parse failed entirely; returning placeholder (raw=%d chars)", len(raw))
    return _placeholder_result(raw)


def enrich(doc: ExtractedDoc, hints: dict | None = None) -> EnrichmentResult:
    """Send text to Ollama, parse with tiered fallback, return enrichment."""
    prompt = _build_prompt(doc, hints or {}, _load_taxonomy())
    log.info("enrich: text=%dchars hints=%s", len(doc.text or ""), sorted((hints or {}).keys()) or "[]")
    try:
        raw = _ollama_generate(prompt)
    except Exception as exc:  # noqa: BLE001
        log.error("Ollama call failed: %s", exc)
        return _placeholder_result(f"<<error>> {exc}")
    result = parse_with_tiers(raw, prompt_for_retry=prompt)
    log.info(
        "enrich done: tier=%s category=%s confidence=%s tags=%d review=%s",
        result.tier, result.category, result.confidence, len(result.tags), result.review,
    )
    return result


def ollama_available() -> bool:
    settings = get_settings()
    try:
        resp = requests.get(settings.ollama.url.rstrip("/") + "/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def ollama_has_model() -> bool:
    settings = get_settings()
    try:
        resp = requests.get(settings.ollama.url.rstrip("/") + "/api/tags", timeout=5)
        if resp.status_code != 200:
            return False
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(m.startswith(settings.ollama.model.split(":", 1)[0]) for m in models)
    except Exception:
        return False
