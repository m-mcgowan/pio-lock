"""Integration smoke test: a real native PlatformIO project builds."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.pio_project import (
    make_native_project,
    piolock_lib,
    requires_pio,
    run_in_pio_python,
    run_pio,
)

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


# ── PioAdapter against real PlatformIO (run under PlatformIO's Python) ───────

_OVERRIDE_INI = """\
[platformio]
extra_configs = pio.lock.ini

[env]
platform = native

[env:a]
platform_packages = tool-user-a @ ^1.0

[env:b]
"""

_OVERRIDE_LOCK_INI = """\
[env:a]
platform = platformio/native@1.2.1
platform_packages = platformio/tool-locked-a@1.0.0

[env:b]
platform = platformio/native@1.1.0
"""

_VIEWS_CODE = """\
import json
from pathlib import Path
import pio_lock

adapter = pio_lock.PioAdapter()
project = Path(sys.argv[1])
out = {}
for view in ("user", "full"):
    cfg = getattr(adapter, view + "_config")(project)
    out[view] = {
        env: [cfg.get("env:" + env, "platform"), cfg.get("env:" + env, "platform_packages", [])]
        for env in ("a", "b")
    }
out["files"] = [str(f) for f in adapter.config_files(project)]
print(json.dumps(out))
"""

_EXPECTED_VIEWS = {
    "user": {"a": ["native", ["tool-user-a @ ^1.0"]], "b": ["native", []]},
    "full": {
        "a": ["platformio/native@1.2.1", ["platformio/tool-locked-a@1.0.0"]],
        "b": ["platformio/native@1.1.0", []],
    },
}


def _override_project(tmp_path: Path) -> Path:
    return make_native_project(
        tmp_path / "proj", _OVERRIDE_INI, files={"pio.lock.ini": _OVERRIDE_LOCK_INI}
    )


@requires_pio
def test_extra_configs_override_platform_per_env(tmp_path: Path) -> None:
    p = _override_project(tmp_path)
    out = run_in_pio_python(_VIEWS_CODE, str(p), cwd=p)
    assert out["user"] == _EXPECTED_VIEWS["user"]
    assert out["full"] == _EXPECTED_VIEWS["full"]
    assert out["files"] == [str(p / "platformio.ini"), str(p / "pio.lock.ini")]


@requires_pio
def test_user_config_view_independent_of_cwd(tmp_path: Path) -> None:
    p = _override_project(tmp_path)
    out = run_in_pio_python(_VIEWS_CODE, str(p), cwd=tmp_path.parent)
    assert out["user"] == _EXPECTED_VIEWS["user"]
    assert out["full"] == _EXPECTED_VIEWS["full"]
    assert out["files"] == [str(p / "platformio.ini"), str(p / "pio.lock.ini")]


_RESOLVE_CODE = """\
import dataclasses
import json
from pathlib import Path
import pio_lock

adapter = pio_lock.PioAdapter()
adapter.probe()
project, env = Path(sys.argv[1]), sys.argv[2]
out = {"core": adapter.core_version()}
for call in ("resolve_platform", "platform_in_use"):
    try:
        result = getattr(adapter, call)(project, env)
        out[call] = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result
    except pio_lock.PlatformNotInstalled as exc:
        out[call] = {"not_installed": str(exc)}
print(json.dumps(out))
"""


def _native_installed_project(tmp_path: Path, ini: str, files: dict[str, str] | None = None):
    p = make_native_project(tmp_path / "proj", ini, files=files)
    install = run_pio(p, "pkg", "install", "-e", "native")
    assert install.returncode == 0, install.stdout
    return p


@requires_pio
def test_resolve_platform_native(tmp_path: Path) -> None:
    p = _native_installed_project(tmp_path, "[env:native]\nplatform = native\n")
    out = run_in_pio_python(_RESOLVE_CODE, str(p), "native", cwd=tmp_path)
    lock = out["resolve_platform"]
    assert lock["name"] == "native"
    assert lock["version"]
    assert lock["spec"] == "native"
    assert lock["platform_packages_spec"] == []
    assert lock["src_url"] is None
    assert out["platform_in_use"] == ["native", lock["version"]]


@requires_pio
def test_resolve_platform_ignores_lock_ini(tmp_path: Path) -> None:
    """resolve_platform uses the user's view; platform_in_use sees pio.lock.ini."""
    ini = "[platformio]\nextra_configs = pio.lock.ini\n[env:native]\nplatform = native\n"
    p = _native_installed_project(tmp_path, ini)
    (p / "pio.lock.ini").write_text(
        "[env:native]\nplatform = native@0.0.1\nplatform_packages = tool-not-installed@9.9.9\n"
    )
    out = run_in_pio_python(_RESOLVE_CODE, str(p), "native", cwd=tmp_path)
    assert out["resolve_platform"]["name"] == "native"
    assert out["resolve_platform"]["packages"] == []
    assert out["platform_in_use"] == {
        "not_installed": "platform for env 'native' is not installed; "
        "run: pio pkg install -e native"
    }


@requires_pio
def test_resolve_platform_package_not_installed(tmp_path: Path) -> None:
    ini = "[env:native]\nplatform = native\nplatform_packages = tool-not-installed@9.9.9\n"
    p = _native_installed_project(tmp_path, "[env:native]\nplatform = native\n")
    (p / "platformio.ini").write_text(ini)
    out = run_in_pio_python(_RESOLVE_CODE, str(p), "native", cwd=tmp_path)
    assert "pio pkg install -e native" in out["resolve_platform"]["not_installed"]
