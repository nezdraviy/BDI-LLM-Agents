"""Locate the existing tools in an installed package or the source checkout."""

from __future__ import annotations

from pathlib import Path
import sys


def ensure_tools_available() -> None:
    # participant4-tools is the supplied source directory, not an importable name.
    # Installation maps data_tools to it; this fallback also supports a checkout.
    source_tools = Path(__file__).resolve().parent.parent / "participant4-tools"
    if (source_tools / "data_tools" / "__init__.py").is_file():
        location = str(source_tools)
        if location not in sys.path:
            sys.path.insert(0, location)
