"""One-shot scrubber: remove all ``.meta/`` sidecar folders under ``documents_root``.

Run once during the migration to the SQLite-backed metadata store. After
this script, the only metadata source of truth is ``data/dragndoc.db``;
the file tree contains only the user's actual documents.

Safety:
- Dry-run by default. Pass ``--apply`` to actually delete.
- Only directories named exactly ``.meta`` are touched.
- The CLI ``.meta`` *file* (the marker that blocks subtree processing) is
  left untouched — only ``.meta`` *directories* are removed.

Usage:
    python scripts/cleanup_sidecars.py            # dry run
    python scripts/cleanup_sidecars.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Make ``dragndoc`` importable when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dragndoc.config import get_settings  # noqa: E402


META_DIR_NAME = ".meta"


def find_meta_dirs(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob(META_DIR_NAME):
        if path.is_dir():
            out.append(path)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete; default is dry-run.")
    parser.add_argument("--root", type=Path, default=None, help="Override DOCUMENTS_ROOT.")
    args = parser.parse_args()

    settings = get_settings()
    root = args.root or settings.documents_root

    if not root.exists():
        print(f"documents_root not found: {root}", file=sys.stderr)
        return 1

    targets = find_meta_dirs(root)
    if not targets:
        print(f"no .meta/ directories found under {root}")
        return 0

    print(f"found {len(targets)} .meta/ director{'y' if len(targets) == 1 else 'ies'} under {root}:")
    for t in targets:
        print(f"  {t}")

    if not args.apply:
        print("\n(dry run; pass --apply to actually delete)")
        return 0

    failures = 0
    for t in targets:
        try:
            shutil.rmtree(t)
            print(f"removed: {t}")
        except OSError as exc:
            print(f"failed: {t} ({exc})", file=sys.stderr)
            failures += 1
    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print(f"\ndone: removed {len(targets)} directory(ies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
