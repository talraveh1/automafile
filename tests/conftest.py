"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Each test gets its own DOCUMENTS_ROOT, STORAGE_DIR, and fresh settings cache."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "Inbox").mkdir()
    monkeypatch.setenv("DOCUMENTS_ROOT", str(docs_root))
    monkeypatch.setenv("INBOX_DIR", "Inbox")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    from dragndoc.config import reset_settings
    reset_settings()
    yield docs_root
    reset_settings()


@pytest.fixture
def docs_root(isolated_env) -> Path:
    return isolated_env


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
