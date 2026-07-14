#!/usr/bin/env python3
"""Validate memoryd through an installed Hermes runtime and real loader."""

from pathlib import Path
import sys


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (_SOURCE_ROOT / "memoryd" / "hermes_validation").is_dir():
    sys.path.insert(0, str(_SOURCE_ROOT))

from memoryd.hermes_validation.installed_runtime import *  # noqa: F401,F403
from memoryd.hermes_validation.installed_runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
