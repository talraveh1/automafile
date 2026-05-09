"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# route test-file .pyc caches to build/pycache so __pycache__ folders don't
# scatter through the source tree before any test module is imported
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / "build" / "pycache")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Each test gets its own DOCS, DATA_DIR, and fresh settings cache."""
    # mirror the default docs/inbox layout while keeping every test isolated
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "Inbox").mkdir()
    monkeypatch.setenv("DOCS", str(docs_root))
    monkeypatch.setenv("INBOX", "Inbox")
    monkeypatch.setenv("LOGS_LEVEL", "WARNING")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from dragndoc.config import reset_settings
    # settings are cached globally, so reset before and after each test
    reset_settings()
    yield docs_root
    reset_settings()


@pytest.fixture
def docs_root(isolated_env) -> Path:
    return isolated_env


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
