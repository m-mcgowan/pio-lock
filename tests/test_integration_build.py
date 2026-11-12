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


def _built_stamp(p: Path) -> str:
    out = subprocess.run(
        [str(p / ".pio/build/native/program")], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _lib_deps_project(tmp_path: Path, legacy: bool) -> Path:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    lib = piolock_lib(p)
    ini = f"[env:native]\nplatform = native\nlib_deps = file://{lib}\n"
    if legacy:
        ini += "extra_scripts = pre:${PROJECT_LIBDEPS_DIR}/${PIOENV}/pio-lock/pio_lock.py\n"
    (p / "platformio.ini").write_text(ini)
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.com",
            "-C",
            str(p),
            "commit",
            "-q",
            "-m",
            "ini",
        ],
        check=True,
        capture_output=True,
    )
    return p


@requires_pio
def test_lib_deps_only_setup(tmp_path: Path) -> None:
    import json
    from datetime import datetime, timezone

    p = _lib_deps_project(tmp_path, legacy=False)
    result = run_pio(p, "run", "-e", "native")
    assert result.returncode == 0, result.stdout
    snap = json.loads((p / "build_snapshot.json").read_text())
    build_date = snap["build_date"]
    dt = datetime.fromisoformat(build_date.replace("Z", "+00:00")).astimezone(timezone.utc)
    expected = dt.strftime("%b %e %Y %H:%M:%S")
    assert _built_stamp(p) == f"built {expected}"
    assert run_pio(p, "run", "-e", "native", "-t", "snapshot-print").returncode == 0


@requires_pio
def test_lib_deps_plus_legacy_pre_line(tmp_path: Path) -> None:
    p = _lib_deps_project(tmp_path, legacy=True)
    result = run_pio(p, "run", "-e", "native")
    assert result.returncode == 0, result.stdout
    assert "deprecated since 0.4.0" in result.stdout
