"""Integration smoke test: a real native PlatformIO project builds."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pio_project import make_native_project, piolock_lib, requires_pio, run_pio

pytestmark = pytest.mark.integration


@requires_pio
def test_native_project_builds(tmp_path: Path) -> None:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    result = run_pio(p, "run", "-e", "native")
    assert result.returncode == 0, result.stdout
    assert (p / ".pio/build/native/program").exists()


@requires_pio
def test_release_mode_snapshot_capture_without_snapshot(tmp_path: Path) -> None:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    lib = piolock_lib(p)
    (p / "platformio.ini").write_text(
        f"[env:native]\nplatform = native\nextra_scripts = pre:{lib}/pio_lock.py\n"
    )
    result = run_pio(
        p,
        "run",
        "-e",
        "native",
        "-t",
        "snapshot-capture",
        env={"PIO_RELEASE_BUILD": "1"},
    )
    assert result.returncode == 0, result.stdout
    assert (p / "build_snapshot.json").exists()
