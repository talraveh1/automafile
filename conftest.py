"""Repo-root conftest.

Sets sys.pycache_prefix early so test-file .pyc caches go under build/pycache
instead of cluttering the source tree with scattered __pycache__ folders.
"""

import sys
from pathlib import Path


sys.pycache_prefix = str(Path(__file__).resolve().parent / "build" / "pycache")
