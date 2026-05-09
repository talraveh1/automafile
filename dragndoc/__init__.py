"""Drag'n'Doc — file-watching, metadata-enriching pipeline."""

import sys as _sys
from pathlib import Path as _Path

# redirect bytecode caches to build/pycache so source trees stay clean
# set before any submodule imports so dragndoc's own .pyc files honor it
_sys.pycache_prefix = str(_Path(__file__).resolve().parent.parent / "build" / "pycache")
del _sys, _Path

__version__ = "0.1.0"
