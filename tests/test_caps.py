"""Tests for adaptive extraction caps."""

from __future__ import annotations

from automafile.extractors._caps import CapConfig, select_pages, trim_to_word_boundary


def test_min_pages_floor_even_when_target_hit_early():
    cfg = CapConfig(min_pages=3, max_pages=5, per_page_chars=100, target_chars=10)
    kept = select_pages(["x" * 50, "y", "z", "extra"], cfg)
    assert kept == ["x" * 50, "y", "z"]


def test_max_pages_ceiling_when_target_never_hits():
    cfg = CapConfig(min_pages=3, max_pages=5, per_page_chars=100, target_chars=1000)
    kept = select_pages((str(i) for i in range(10)), cfg)
    assert kept == ["0", "1", "2", "3", "4"]


def test_target_stop_does_not_pull_next_page():
    pulled: list[int] = []

    def pages():
        for i, text in enumerate(["a" * 40, "b" * 40, "c" * 40, "d" * 100, "e" * 100]):
            pulled.append(i)
            yield text

    cfg = CapConfig(min_pages=3, max_pages=5, per_page_chars=100, target_chars=200)
    kept = select_pages(pages(), cfg)
    assert len(kept) == 4
    assert pulled == [0, 1, 2, 3]


def test_per_page_trim_applies_to_each_page():
    cfg = CapConfig(min_pages=1, max_pages=2, per_page_chars=5, target_chars=100)
    kept = select_pages(["abcdef", "ghijkl"], cfg)
    assert kept == ["abcde", "ghijk"]


def test_word_boundary_trim_uses_recent_whitespace():
    text = "alpha beta gamma delta"
    assert trim_to_word_boundary(text, 20) == "alpha beta gamma"


def test_hard_cut_for_long_unbroken_text():
    assert trim_to_word_boundary("x" * 100, 20) == "x" * 20
    assert trim_to_word_boundary("漢字" * 50, 20) == "漢字" * 10


def test_empty_pages_count_toward_min_not_target():
    cfg = CapConfig(min_pages=3, max_pages=5, per_page_chars=100, target_chars=10)
    kept = select_pages(["", "", "", "x" * 20, "extra"], cfg)
    assert kept == ["", "", "", "x" * 20]
