"""Compatibility launcher for the rebuilt music event bot."""

# ruff: noqa: E402,I001

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from music_event_bot.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
