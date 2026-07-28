#!/usr/bin/env python3
"""Claude Code status-line entry point for the Quota Glass bridge."""

import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from app.providers.claude_statusline import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
