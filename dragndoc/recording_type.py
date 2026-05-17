"""Per-recording classification: phone_call / conversation / speech / music / non_speech / unknown.

Pure signal aggregation — no LLM call here, no schema mutations. Returns
a tuple of ``(committed_type, proposed_type, proposal_payload)`` so the
caller decides whether to write the committed value directly (when
ground-truth like mutagen tags or channels) or queue a proposal for
user review (path-pattern guesses, weak signals).

Signals consumed (priority order):

1. **mutagen tags** — non-empty ``genre``/``artist``/``album``/``tracknumber``
   → ``music``. Strongest non-channel evidence.
2. **path patterns** from ``asr.path_patterns`` — regex matches the file's
   docs-tree-relative path. Returns the pattern's ``recording_type`` +
   ``speakers`` map.
3. **channel count** — ≥ 2 → ``phone_call`` (channel-split already
   gives ground-truth separation).
4. **VAD speech ratio** from the first 30s — < 0.2 → ``non_speech``.
5. **fallback** — ``unknown`` (treated as ``conversation`` for behavior).

Path patterns use named regex groups; ``{owner}`` resolves from
``asr.owner_name``, named groups (e.g. ``{other}``) from the regex match.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.log import get_logger


log = get_logger(__name__)


RECORDING_TYPES = (
    "phone_call",
    "conversation",
    "speech",
    "music",
    "non_speech",
    "unknown",
)


# ---------------------------------------------------------------------------
# path patterns
# ---------------------------------------------------------------------------


def _docs_relative_path(path: Path) -> str:
    """Return ``path`` as a forward-slash relative path under the docs root."""
    from dragndoc.meta_store import relative_to_root
    return relative_to_root(path)


def _substitute_speakers(template_map: dict[str, str], regex_groups: dict[str, str]) -> dict[str, str]:
    """Replace ``{owner}`` / ``{other}`` / etc. tokens in speaker template values."""
    settings = get_settings()
    bindings: dict[str, str] = {"owner": settings.asr.owner_name or ""}
    bindings.update({k: v for k, v in regex_groups.items() if v})
    out: dict[str, str] = {}
    for label, template in template_map.items():
        rendered = template
        for k, v in bindings.items():
            rendered = rendered.replace(f"{{{k}}}", v)
        if rendered:
            out[label] = rendered
    return out


def match_path_pattern(path: Path) -> dict[str, Any] | None:
    """Try every entry in ``asr.path_patterns`` against the docs-relative path.

    Returns the first matching pattern's metadata as a dict with rendered
    speaker template values, or ``None`` if no pattern fires.
    """
    settings = get_settings()
    patterns = settings.asr.path_patterns or []
    if not patterns:
        return None
    rel = _docs_relative_path(path)
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        if not pattern:
            continue
        try:
            match = re.search(pattern, rel)
        except re.error as exc:
            log.warning("Bad path-pattern regex %r: %s", pattern, exc)
            continue
        if not match:
            continue
        groups = match.groupdict()
        speakers_tpl = entry.get("speakers") or {}
        rendered = _substitute_speakers(speakers_tpl, groups)
        return {
            "pattern": pattern,
            "recording_type": entry.get("recording_type") or "unknown",
            "speakers": rendered,
            "groups": groups,
        }
    return None


# ---------------------------------------------------------------------------
# mutagen signal
# ---------------------------------------------------------------------------


def _is_music_by_tags(audio_metadata: dict[str, Any]) -> bool:
    """Look at mutagen-derived metadata for music indicators."""
    if not audio_metadata:
        return False
    # mutagen tag names vary by format (TPE1, artist, ARTIST, ...). Check
    # common ones case-insensitively across our `audio_*` prefixed keys.
    candidates: set[str] = set()
    for k in audio_metadata.keys():
        key = k.lower()
        if key.startswith("audio_"):
            key = key[len("audio_"):]
        candidates.add(key)
    music_signals = {
        "genre", "tcon",                # genre
        "artist", "tpe1", "tpe2",       # artist
        "album", "talb",                # album
        "tracknumber", "trck",          # track number
    }
    return bool(candidates & music_signals)


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


def classify(
    path: Path,
    *,
    audio_metadata: dict[str, Any] | None = None,
    channels: int | None = None,
    vad_speech_ratio: float | None = None,
) -> dict[str, Any]:
    """Aggregate signals and return a classification dict.

    Return shape::

        {
          "recording_type": "phone_call" | … | "unknown",
          "source": "mutagen" | "path_pattern" | "channel" | "vad" | "default",
          "rationale": "…",
          "committed": True | False,    # True when the signal is ground-truth
          "speakers": {label: name, …}, # from path pattern, if any
        }

    ``committed=True`` means the caller should write the value directly to
    ``asr.recording_type`` without queueing a proposal. ``committed=False``
    means: enqueue a ``recording_type`` proposal and let the user accept.
    """
    audio_metadata = audio_metadata or {}

    # 1. mutagen tags — strong, committed
    if _is_music_by_tags(audio_metadata):
        return {
            "recording_type": "music",
            "source": "mutagen",
            "rationale": "non-empty artist/album/genre tag",
            "committed": True,
            "speakers": {},
        }

    # 2. path pattern — guess for user to confirm
    pat = match_path_pattern(path)
    if pat is not None:
        rec_type = pat["recording_type"] or "unknown"
        return {
            "recording_type": rec_type,
            "source": "path_pattern",
            "rationale": f"matched pattern: {pat['pattern']}",
            "committed": False,
            "speakers": pat["speakers"] or {},
        }

    # 3. channels — committed (channel-split gives ground truth)
    if channels is not None and channels >= 2:
        return {
            "recording_type": "phone_call",
            "source": "channel",
            "rationale": f"{channels}-channel recording (stereo call)",
            "committed": True,
            "speakers": {},
        }

    # 4. VAD speech ratio — non-speech is committed (no speech, no transcript)
    if vad_speech_ratio is not None and vad_speech_ratio < 0.2:
        return {
            "recording_type": "non_speech",
            "source": "vad",
            "rationale": f"speech ratio {vad_speech_ratio:.2f} < 0.2",
            "committed": True,
            "speakers": {},
        }

    # 5. fallback
    return {
        "recording_type": "unknown",
        "source": "default",
        "rationale": "no signals fired",
        "committed": False,
        "speakers": {},
    }
