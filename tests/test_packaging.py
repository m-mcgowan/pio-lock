"""Packaging metadata consistency."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_library_json_version_matches_pyproject() -> None:
    lib = json.loads((ROOT / "library.json").read_text())
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert match
    assert lib["version"] == match.group(1)
    assert lib["build"]["extraScript"] == "pio_lock.py"
