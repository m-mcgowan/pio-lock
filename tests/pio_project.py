"""Helpers for integration tests that build real native PlatformIO projects."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MAIN = """\
#include <stdio.h>

int main() {
    printf("built %s %s\\n", __DATE__, __TIME__);
    return 0;
}
"""


def have_pio() -> bool:
    """Check if the PlatformIO CLI is available."""
    try:
        subprocess.run(["pio", "--version"], capture_output=True, check=True, timeout=30)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_pio = pytest.mark.skipif(not have_pio(), reason="PlatformIO CLI not available")


def _git(project: Path, *args: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *identity, "-C", str(project), *args],
        check=True,
        capture_output=True,
    )


def make_native_project(root: Path, ini: str, files: dict[str, str] | None = None) -> Path:
    """Write a native PlatformIO project under ``root``, git-init and commit it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "platformio.ini").write_text(ini)
    all_files = {"src/main.cpp": DEFAULT_MAIN}
    all_files.update(files or {})
    for rel, content in all_files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def piolock_lib(root: Path) -> Path:
    """Copy pio_lock.py (and library.json if present) to ``root/piolock-lib``."""
    lib = root / "piolock-lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("pio_lock.py", "library.json"):
        src = REPO_ROOT / name
        if src.exists():
            shutil.copy2(src, lib / name)
    return lib


def run_pio(
    project: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run ``pio <args> -d <project>`` with stdout and stderr combined."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["pio", *args, "-d", str(project)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=full_env,
        timeout=timeout,
    )
