"""Packaging metadata consistency, and running without PlatformIO."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "pio_lock.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_library_json_version_matches_pyproject() -> None:
    lib = json.loads((ROOT / "library.json").read_text())
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert match
    assert lib["version"] == match.group(1)
    assert lib["build"]["extraScript"] == "pio_lock.py"


# ── Without PlatformIO ───────────────────────────────────────────────────────
#
# pio_lock.py must import and lock libraries on a Python that has no PlatformIO (the unit-test
# CI job, a plain `pip install pio-lock`). These tests block `platformio` in a fresh interpreter,
# so they hold even where PlatformIO is installed.

_BLOCK_PIO = "import sys; sys.modules['platformio'] = None\n"


def _run_without_pio(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    # PIO_LOCK_REEXEC stops the CLI re-exec'ing under PlatformIO's Python, which would undo
    # the block on a machine that has PlatformIO.
    env = {**os.environ, "PIO_LOCK_REEXEC": "1"}
    return subprocess.run(
        [sys.executable, "-I", "-c", _BLOCK_PIO + code, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=60,
    )


def _run_cli_without_pio(*argv: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import runpy\nsys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    return _run_without_pio(code, str(SCRIPT), *argv)


def test_module_imports_without_platformio() -> None:
    code = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('pio_lock', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['pio_lock'] = module\n"
        "spec.loader.exec_module(module)\n"
        "assert callable(module.register_pio_targets)\n"
        "assert callable(module.main)\n"
        "print('imported')\n"
    )
    result = _run_without_pio(code)
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


def test_cli_help_without_platformio() -> None:
    result = _run_cli_without_pio("--help")
    assert result.returncode == 0, result.stderr
    assert "capture" in result.stdout


def test_cli_locks_libraries_without_platformio(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "registry-exact", project)
    (project / "pio.lock.json").unlink()
    (project / "pio.lock.ini").unlink()

    result = _run_cli_without_pio("capture", "-d", str(project), "-e", "native")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "platform locking disabled" in result.stderr
    lock = json.loads((project / "pio.lock.json").read_text())
    libraries = lock["envs"]["native"]["libraries"]
    assert [lib["name"] for lib in libraries] == ["SparkFun MAX1704x Fuel Gauge Arduino Library"]
    assert "platform" not in lock["envs"]["native"]

    check = _run_cli_without_pio("check", "-d", str(project), "-e", "native")
    assert check.returncode == 0, check.stdout + check.stderr
