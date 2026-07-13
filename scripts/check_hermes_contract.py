#!/usr/bin/env python3
"""Check memoryd against the pinned Hermes Agent MemoryProvider contract."""

from pathlib import Path
import sys


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (_SOURCE_ROOT / "memoryd" / "hermes_validation").is_dir():
    sys.path.insert(0, str(_SOURCE_ROOT))

from memoryd.hermes_validation.contract import *  # noqa: F401,F403
from memoryd.hermes_validation.contract import main


if __name__ == "__main__":
    raise SystemExit(main())
