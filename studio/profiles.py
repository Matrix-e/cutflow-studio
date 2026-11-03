"""Per-import editing choices encoded in an explicit inbox subdirectory.

Only the first two exact POSIX path components carry a profile. Names elsewhere
in the inbox never imply an editing mode or change source identity.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re


IMPORT_ROOT = "工作台导入"
IMPORT_MODES = frozenset({"gentle", "preserve", "library"})


def import_mode(source: str) -> str | None:
    parts = source.split("/")
    if len(parts) >= 4 and parts[0] == IMPORT_ROOT and parts[1] in IMPORT_MODES:
        return parts[1]
    return None


def effective_settings(source: str, settings: dict) -> dict:
    result = dict(settings)
    if import_mode(source) == "preserve":
        # Manual cut ranges and fixed dimensions would contradict this mode.
        for field in ("source_ranges", "width", "height"):
            result.pop(field, None)
        result.update(remove_silence=False, normalize_audio=True, target_aspect="original")
    return result


def display_name(source: str) -> str:
    """Strip only the content suffix produced by our SFTP sync, for display."""
    name = PurePosixPath(source).name
    path = PurePosixPath(name)
    stem = re.sub(r"--[0-9a-f]{16}$", "", path.stem)
    return stem + path.suffix
