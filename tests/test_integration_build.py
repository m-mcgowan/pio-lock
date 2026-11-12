"""Integration smoke test: a real native PlatformIO project builds."""

from __future__ import annotations

import subprocess
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


@requires_pio
def test_snapshot_check_reports_stale_in_release_mode(tmp_path: Path) -> None:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    lib = piolock_lib(p)
    ini = f"[env:native]\nplatform = native\nextra_scripts = pre:{lib}/pio_lock.py\n"
    (p / "platformio.ini").write_text(ini)
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    commit = ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "-C", str(p)]
    subprocess.run([*commit, "commit", "-q", "-m", "ini"], check=True, capture_output=True)
    assert run_pio(p, "run", "-e", "native", "-t", "snapshot-capture").returncode == 0
    snap = p / "build_snapshot.json"
    (p / "extra.txt").write_text("x")
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    subprocess.run([*commit, "commit", "-q", "-m", "two"], check=True, capture_output=True)
    before = snap.read_bytes()
    result = run_pio(
        p, "run", "-e", "native", "-t", "snapshot-check", env={"PIO_RELEASE_BUILD": "1"}
    )
    assert result.returncode != 0, result.stdout
    assert "STALE" in result.stdout
    assert snap.read_bytes() == before
