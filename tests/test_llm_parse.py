"""Tests for the tiered LLM JSON parser."""

from __future__ import annotations

from automafile.llm import (
    HEBREW_GERSHAYIM,
    parse_with_tiers,
    sanitize_excerpt,
)


VALID_JSON = """\
{"title":"Invoice 12","summary":"A short summary.","tags":["bill","monthly"],
 "category":"Financial","subcategory":null,"correspondent":"Acme Corp",
 "date":"2026-04-01","amount":120.5,"currency":"USD","language":"en",
 "confidence":"high","needs_review":false,"reason":"clear invoice"}
"""

UNESCAPED_QUOTE_JSON = """\
{"title":"Note","summary":"He said "hi" to me.","tags":["chat"],
 "category":"Personal","subcategory":null,"correspondent":null,
 "date":null,"amount":null,"currency":null,"language":"en",
 "confidence":"medium","needs_review":false,"reason":null}
"""

GARBAGE = "blah blah {not json"


def test_strict_parse_succeeds():
    result = parse_with_tiers(VALID_JSON)
    assert result.tier == "strict"
    assert result.title == "Invoice 12"
    assert result.summary == "A short summary."
    assert result.tags == ["bill", "monthly"]
    assert result.category == "Financial"
    assert result.amount == 120.5
    assert result.currency == "USD"
    assert result.confidence == "high"
    assert result.needs_review is False


def test_repair_fixes_unescaped_inner_quotes():
    result = parse_with_tiers(UNESCAPED_QUOTE_JSON)
    # repair tier should rescue this without a network round-trip
    assert result.tier in {"repair", "regex"}
    assert result.category == "Personal"
    assert "hi" in (result.summary or "")


def test_garbage_falls_through_to_placeholder():
    result = parse_with_tiers(GARBAGE)
    assert result.tier in {"placeholder", "regex"}
    assert result.category == "Unknown"
    assert result.needs_review is True


def test_hebrew_quote_sanitization():
    raw = 'הודעה: "שלום" '
    out = sanitize_excerpt(raw)
    assert HEBREW_GERSHAYIM in out


def test_category_alias_medical_to_personal():
    raw = '{"title":"prescription","summary":"hi","tags":["rx"],"category":"medical","subcategory":null,"correspondent":null,"date":null,"amount":null,"currency":null,"language":"en","confidence":"medium","needs_review":false,"reason":null}'
    result = parse_with_tiers(raw)
    assert result.category == "Personal"
