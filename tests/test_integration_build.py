"""Integration smoke test: a real native PlatformIO project builds."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.pio_project import (
    REPO_ROOT,
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


_PROJECT_DIR_CODE = """\
import dataclasses
import json
from pathlib import Path
import pio_lock

adapter = pio_lock.PioAdapter()
project = Path(sys.argv[1])
out = {}
for view in ("user", "full"):
    cfg = getattr(adapter, view + "_config")(project)
    out[view] = cfg.get("env:native", "platform_packages", [])
out["resolve"] = dataclasses.asdict(adapter.resolve_platform(project, "native"))
out["in_use"] = adapter.platform_in_use(project, "native")
print(json.dumps(out))
"""


@requires_pio
def test_project_dir_interpolation_independent_of_cwd(tmp_path: Path) -> None:
    """${PROJECT_DIR} expands to the project, not the caller's CWD (PIO expands it at get())."""
    p = _native_installed_project(tmp_path, "[env:native]\nplatform = native\n").resolve()
    # A local tool "installed" into a project-local packages_dir (no network, no global state).
    tool_uri = f"file://{p}/tools/mytool"
    pkg = p / ".pkgs" / "tool-mytool"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "tool-mytool", "version": "1.2.3"}')
    piopm = {
        "type": "tool",
        "name": "tool-mytool",
        "version": "1.2.3",
        "spec": {
            "owner": None,
            "id": None,
            "name": "tool-mytool",
            "requirements": None,
            "uri": tool_uri,
        },
    }
    (pkg / ".piopm").write_text(json.dumps(piopm))
    (p / "platformio.ini").write_text(
        "[platformio]\npackages_dir = ${PROJECT_DIR}/.pkgs\n"
        "[env:native]\nplatform = native\n"
        "platform_packages = tool-mytool @ file://${PROJECT_DIR}/tools/mytool\n"
    )
    out = run_in_pio_python(_PROJECT_DIR_CODE, str(p), cwd=tmp_path.parent)
    expected = [f"tool-mytool @ {tool_uri}"]
    assert out["user"] == expected
    assert out["full"] == expected
    assert out["resolve"]["platform_packages_spec"] == expected
    assert out["resolve"]["packages"] == [
        {"name": "tool-mytool", "version": "1.2.3", "owner": None, "src_url": tool_uri}
    ]
    assert out["in_use"][0] == "native"


# ── Running the CLI from a Python without PlatformIO ─────────────────────────

_NOT_FOUND = "PlatformIO's Python not found"


@pytest.fixture(scope="module")
def bare_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh venv without platformio, with pio-lock pip-installed from this checkout.

    The package is built from a copy of the checkout so the build doesn't write
    ``build/`` or ``*.egg-info`` into the repo.
    """
    root = tmp_path_factory.mktemp("bare")
    src = root / "src"
    src.mkdir()
    for name in ("pio_lock.py", "pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, src / name)
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    subprocess.run(
        [str(venv / "bin/pip"), "install", "-q", str(src)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    probe = subprocess.run([str(venv / "bin/python"), "-c", "import platformio"], check=False)
    assert probe.returncode != 0, "bare venv unexpectedly has platformio"
    return venv


def _run_cli(
    cmd: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {k: v for k, v in os.environ.items() if k != "PIO_LOCK_REEXEC"}
    full_env.pop("GITHUB_TOKEN", None)
    full_env.update(env or {})
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=full_env, timeout=600
    )


def _cli_variants(venv: Path) -> dict[str, list[str]]:
    return {
        "script": [str(venv / "bin/python"), str(REPO_ROOT / "pio_lock.py")],
        "entry_point": [str(venv / "bin/pio-lock")],
    }


def _lockfile(p: Path) -> dict:
    return json.loads((p / "pio.lock.json").read_text())


@requires_pio
@pytest.mark.parametrize("variant", ["script", "entry_point"])
def test_cli_from_python_without_platformio(bare_venv: Path, tmp_path: Path, variant: str) -> None:
    """Covers test_cli_from_python_without_platformio and
    test_installed_entry_point_without_platformio: the CLI re-runs under PlatformIO's Python."""
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    cli = _cli_variants(bare_venv)[variant]
    result = _run_cli([*cli, "-d", str(p), "capture", "-e", "native"])
    assert result.returncode == 0, result.stdout
    assert _NOT_FOUND not in result.stdout
    assert "libraries" in _lockfile(p)["envs"]["native"]
    # outdated imports platformio in-process: it only works if the CLI re-ran under PIO's Python.
    outdated = _run_cli([*cli, "-d", str(p), "outdated", "-e", "native", "--json"])
    assert outdated.returncode == 0, outdated.stdout
    assert "PlatformIO Python package is required" not in outdated.stdout


@requires_pio
@pytest.mark.parametrize("variant", ["script", "entry_point"])
def test_cli_without_platformio_locks_platform(
    bare_venv: Path, tmp_path: Path, variant: str
) -> None:
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    assert run_pio(p, "pkg", "install", "-e", "native").returncode == 0
    cli = _cli_variants(bare_venv)[variant]
    result = _run_cli([*cli, "-d", str(p), "capture", "-e", "native"])
    assert result.returncode == 0, result.stdout
    assert _lockfile(p)["envs"]["native"]["platform"]["name"] == "native"
    assert (p / "pio.lock.ini").exists()


@requires_pio
def test_reexec_not_found_still_locks_libraries(bare_venv: Path, tmp_path: Path) -> None:
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    no_pio_path = f"{bare_venv / 'bin'}:/usr/bin:/bin"
    result = _run_cli(
        [str(bare_venv / "bin/pio-lock"), "-d", str(p), "capture", "-e", "native"],
        env={"PATH": no_pio_path},
    )
    assert result.returncode == 0, result.stdout
    assert _NOT_FOUND in result.stdout
    env_lock = _lockfile(p)["envs"]["native"]
    assert "libraries" in env_lock
    assert "platform" not in env_lock


@requires_pio
def test_reexec_does_not_loop(bare_venv: Path, tmp_path: Path) -> None:
    """With PIO_LOCK_REEXEC already set, a Python without platformio never re-execs."""
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    result = _run_cli(
        [str(bare_venv / "bin/pio-lock"), "-d", str(p), "capture", "-e", "native"],
        env={"PIO_LOCK_REEXEC": "1"},
    )
    assert result.returncode == 0, result.stdout
    assert _NOT_FOUND in result.stdout
    env_lock = _lockfile(p)["envs"]["native"]
    assert "libraries" in env_lock
    assert "platform" not in env_lock


@requires_pio
def test_capture_native_end_to_end(tmp_path: Path) -> None:
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    assert run_pio(p, "pkg", "install", "-e", "native").returncode == 0
    result = _run_cli(
        [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p), "capture", "-e", "native"]
    )
    assert result.returncode == 0, result.stdout
    data = _lockfile(p)
    assert data["lockfile_version"] == 2
    platform = data["envs"]["native"]["platform"]
    assert platform["name"] == "native"
    assert platform["spec"] == "native"
    ini = (p / "pio.lock.ini").read_text()
    assert f"platform = platformio/native@{platform['version']}\n" in ini


@requires_pio
def test_capture_platform_inherited_from_env_section_real_pio(tmp_path: Path) -> None:
    p = make_native_project(tmp_path / "proj", "[env]\nplatform = native\n[env:a]\n[env:b]\n")
    assert run_pio(p, "pkg", "install", "-e", "a").returncode == 0
    cmd = [
        sys.executable,
        str(REPO_ROOT / "pio_lock.py"),
        "-d",
        str(p),
        "capture",
        "-e",
        "a",
        "-e",
        "b",
    ]
    result = _run_cli(cmd)
    assert result.returncode == 0, result.stdout
    envs = _lockfile(p)["envs"]
    assert envs["a"]["platform"]["spec"] == "native"
    assert envs["b"]["platform"]["spec"] == "native"


@requires_pio
def test_check_warn_only_on_real_project(tmp_path: Path) -> None:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    cli = [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p)]

    def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*cli, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **(env or {})},
            timeout=600,
        )

    captured = run("capture", "-e", "native")
    assert captured.returncode == 0, captured.stdout
    ok = run("check", "-e", "native")
    assert ok.returncode == 0, ok.stdout

    (p / "platformio.ini").write_text("[env:native]\nplatform = native@*\n")
    failed = run("check", "-e", "native")
    assert failed.returncode == 1, failed.stdout
    assert "DRIFT:" in failed.stdout

    warned = run("check", "-e", "native", "--warn-only")
    assert warned.returncode == 0, warned.stdout
    assert "WARNING:" in warned.stdout
    assert "DRIFT:" not in warned.stdout

    env_warned = run("check", "-e", "native", env={"PIO_LOCK_DRIFT": "warn"})
    assert env_warned.returncode == 0, env_warned.stdout
    assert "WARNING:" in env_warned.stdout


@requires_pio
def test_real_adapter_package_installed(tmp_path: Path) -> None:
    # The native platform has no packages of its own, so probe whatever tool is installed.
    code = """
import json, sys
from pio_lock import PioAdapter
from platformio.package.manager.tool import ToolPackageManager
adapter = PioAdapter()
installed = ToolPackageManager().get_installed()
if not installed:
    print(json.dumps({"skip": True}))
    sys.exit(0)
meta = installed[0].metadata
real = {"name": meta.name, "version": str(meta.version), "owner": meta.spec.owner}
print(json.dumps({
    "skip": False,
    "exact": adapter.package_installed(real),
    "wrong": adapter.package_installed({**real, "version": "0.0.0-nope"}),
}))
"""
    out = run_in_pio_python(code, cwd=tmp_path)
    if out["skip"]:
        pytest.skip("no tool packages installed")
    assert out["exact"] is True
    assert out["wrong"] is False


@requires_pio
def test_check_malformed_ini_is_clean_error(tmp_path: Path) -> None:
    p = make_native_project(tmp_path, "[env:native]\nplatform = native\n")
    cli = [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p)]
    assert subprocess.run([*cli, "capture", "-e", "native"], capture_output=True).returncode == 0
    (p / "platformio.ini").write_text("[env:native\nplatform = native\n")
    result = subprocess.run(
        [*cli, "check", "-e", "native"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode == 1, result.stdout
    assert "Error: invalid PlatformIO project configuration" in result.stdout
    assert "Traceback" not in result.stdout


# ── Build-time drift checks (pio.lock.ini included) ──────────────────────────


def _commit_and_snapshot(p: Path) -> None:
    """Commit the project and refresh build_snapshot.json, so release builds accept it."""
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            *["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "-C", str(p)],
            *["commit", "-q", "--allow-empty", "-m", "update"],
        ],
        check=True,
        capture_output=True,
    )
    result = run_pio(p, "run", "-e", "native", "-t", "snapshot-capture")
    assert result.returncode == 0, result.stdout


def _locked_build_project(tmp_path: Path) -> tuple[Path, str]:
    """A native project wired to pio-lock, captured and with pio.lock.ini included.

    Returns the project and its [env:native] body (without the platform line).
    """
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    lib = piolock_lib(p)
    (p / ".gitignore").write_text(".pio/\nbuild_snapshot.json\n")
    env_body = f"extra_scripts = pre:{lib}/pio_lock.py\n"
    (p / "platformio.ini").write_text(f"[env:native]\nplatform = native\n{env_body}")
    assert run_pio(p, "pkg", "install", "-e", "native").returncode == 0
    cli = [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p)]
    for args in (["capture", "-e", "native"], ["init"]):
        result = _run_cli([*cli, *args])
        assert result.returncode == 0, result.stdout
    _commit_and_snapshot(p)
    return p, env_body


def _set_env(p: Path, env_body: str, platform: str, extra: str = "") -> None:
    """Rewrite [env:native] keeping the [platformio] section init added."""
    ini = (p / "platformio.ini").read_text()
    head = ini[: ini.index("[env:native]")]
    (p / "platformio.ini").write_text(
        f"{head}[env:native]\nplatform = {platform}\n{env_body}{extra}"
    )
    _commit_and_snapshot(p)


_RELEASE = {"PIO_RELEASE_BUILD": "1"}


@requires_pio
def test_lock_ini_end_to_end(tmp_path: Path) -> None:
    p, env_body = _locked_build_project(tmp_path)
    ok = run_pio(p, "run", "-e", "native")
    assert ok.returncode == 0, ok.stdout
    assert "compared with pio.lock.json" not in ok.stdout
    assert "extra_configs = pio.lock.ini' (or run" not in ok.stdout
    released = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert released.returncode == 0, released.stdout

    _set_env(p, env_body, "native@*")
    stale = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert stale.returncode != 0, stale.stdout
    assert "platform spec changed" in stale.stdout
    assert "run: pio-lock capture --install -e native" in stale.stdout


def _local_platform(root: Path, name: str = "piolocktest") -> str:
    """A minimal platform in a local directory (nothing to download); returns its file:// spec."""
    platform = root / f"{name}-platform"
    (platform / "builder").mkdir(parents=True)
    (platform / "platform.json").write_text(
        json.dumps({"name": name, "version": "0.0.1", "engines": {"platformio": "^6"}})
    )
    (platform / "builder" / "main.py").write_text('Import("env")\n')
    return platform.as_uri()


@requires_pio
def test_capture_install_records_a_changed_platform(tmp_path: Path) -> None:
    """With pio.lock.ini included, `pio pkg install -e <env>` installs the LOCKED platform, so
    a changed `platform =` can only be captured with `capture --install`."""
    p, env_body = _locked_build_project(tmp_path)
    spec = _local_platform(tmp_path)
    _set_env(p, env_body, spec)
    cli = [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p)]
    try:
        assert run_pio(p, "pkg", "install", "-e", "native").returncode == 0
        plain = _run_cli([*cli, "capture", "-e", "native"])
        assert plain.returncode == 1, plain.stdout
        assert "run: pio-lock capture --install -e native" in plain.stdout

        installed = _run_cli([*cli, "capture", "--install", "-e", "native"])
        assert installed.returncode == 0, installed.stdout
        platform = _lockfile(p)["envs"]["native"]["platform"]
        assert (platform["spec"], platform["name"], platform["version"]) == (
            spec,
            "piolocktest",
            "0.0.1",
        )
        assert f"platform = {spec}\n" in (p / "pio.lock.ini").read_text()
    finally:
        subprocess.run(
            ["pio", "pkg", "uninstall", "-g", "-p", "piolocktest"],
            capture_output=True,
            timeout=300,
        )


@requires_pio
def test_release_build_drift_env_var_warns(tmp_path: Path) -> None:
    p, env_body = _locked_build_project(tmp_path)
    _set_env(p, env_body, "native@*")
    result = run_pio(p, "run", "-e", "native", env={**_RELEASE, "PIO_LOCK_DRIFT": "warn"})
    assert result.returncode == 0, result.stdout
    assert "WARNING: platform spec changed" in result.stdout


@requires_pio
def test_release_build_drift_ini_option_warns(tmp_path: Path) -> None:
    p, env_body = _locked_build_project(tmp_path)
    _set_env(p, env_body, "native@*", extra="custom_pio_lock_drift = warn\n")
    env = {**_RELEASE}
    result = subprocess.run(
        ["pio", "run", "-e", "native", "-d", str(p)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={k: v for k, v in {**os.environ, **env}.items() if k != "PIO_LOCK_DRIFT"},
        timeout=600,
    )
    assert result.returncode == 0, result.stdout
    assert "WARNING: platform spec changed" in result.stdout


@requires_pio
def test_release_build_override_allowed(tmp_path: Path) -> None:
    p, _ = _locked_build_project(tmp_path)
    (p / "late.ini").write_text("[env:native]\nplatform = native\n")
    ini = (p / "platformio.ini").read_text()
    (p / "platformio.ini").write_text(
        ini.replace("extra_configs = pio.lock.ini", "extra_configs = pio.lock.ini\n  late.ini")
    )
    assert "late.ini" in (p / "platformio.ini").read_text()
    _commit_and_snapshot(p)

    failed = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert failed.returncode != 0, failed.stdout
    assert "late.ini sets platform for env 'native' after pio.lock.ini" in failed.stdout

    allowed = run_pio(p, "run", "-e", "native", env={**_RELEASE, "PIO_LOCK_ALLOW_OVERRIDE": "1"})
    assert allowed.returncode == 0, allowed.stdout
    assert "WARNING: late.ini sets platform" in allowed.stdout


_PROMPT = "Continue with this build? [c]ontinue / [a]bort:"


def _pio_run_in_pty(p: Path, answer: str, timeout: float = 600) -> tuple[int, str]:
    """Run ``pio run`` with a pseudo-terminal as stdin/stdout; type ``answer`` at the prompt."""
    pty = pytest.importorskip("pty")
    import select
    import time

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["pio", "run", "-e", "native", "-d", str(p)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={k: v for k, v in os.environ.items() if k != "PIO_RELEASE_BUILD"},
        close_fds=True,
    )
    os.close(slave)
    output = b""
    answered = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: the child closed the terminal
                    break
                if not chunk:
                    break
                output += chunk
            elif proc.poll() is not None:
                break
            if not answered and _PROMPT.encode() in output:
                os.write(master, answer.encode())
                answered = True
        rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return rc, output.decode(errors="replace")


@requires_pio
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")
def test_interactive_build_abort_under_pty(tmp_path: Path) -> None:
    p, env_body = _locked_build_project(tmp_path)
    _set_env(p, env_body, "native@*")
    rc, out = _pio_run_in_pty(p, "a\n")
    assert _PROMPT in out, out
    assert rc != 0, out


@requires_pio
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")
def test_interactive_build_continue_under_pty(tmp_path: Path) -> None:
    p, env_body = _locked_build_project(tmp_path)
    _set_env(p, env_body, "native@*")
    rc, out = _pio_run_in_pty(p, "c\n")
    assert _PROMPT in out, out
    assert rc == 0, out
    assert "SUCCESS" in out


# ── Duplicate library copies ─────────────────────────────────────────────────


def _install_fake_copy(p: Path, dir_name: str, version: str, function: str) -> None:
    """A registry-style installed copy of ``piolocktest/dupdemo`` (no network needed)."""
    lib = p / ".pio" / "libdeps" / "native" / dir_name
    (lib / "src").mkdir(parents=True)
    piopm = {
        "type": "library",
        "name": "dupdemo",
        "version": version,
        "spec": {"owner": "piolocktest", "id": None, "name": "dupdemo", "requirements": None},
    }
    (lib / ".piopm").write_text(json.dumps(piopm))
    (lib / "library.json").write_text(json.dumps({"name": "dupdemo", "version": version}))
    (lib / "src" / f"{function}.c").write_text(f"int {function}(void) {{ return 1; }}\n")


def _duplicates_project(
    tmp_path: Path, lib_deps: list[str], main: str, *, via_lib_deps: bool = False
) -> Path:
    """dupdemo 1.1.0 (``dupdemo/``) and 1.0.0 (``dupdemo@1.0.0/``) installed side by side.

    pio-lock is loaded with an ``extra_scripts`` line, or (``via_lib_deps``) from lib_deps,
    where its library.json runs it as an extra script in a library builder's environment.
    """
    p = make_native_project(tmp_path / "proj", "[env:native]\nplatform = native\n")
    lib = piolock_lib(p)
    (p / ".gitignore").write_text(".pio/\nbuild_snapshot.json\n")
    specs = [*lib_deps, f"file://{lib}"] if via_lib_deps else lib_deps
    loader = "" if via_lib_deps else f"extra_scripts = pre:{lib}/pio_lock.py\n"
    deps = "".join(f"\n    {spec}" for spec in specs)
    (p / "platformio.ini").write_text(
        f"[env:native]\nplatform = native\n{loader}lib_deps ={deps}\n"
    )
    (p / "src" / "main.cpp").write_text(main)
    _install_fake_copy(p, "dupdemo", "1.1.0", "dupdemo_new")
    _install_fake_copy(p, "dupdemo@1.0.0", "1.0.0", "dupdemo_old")
    installed = run_pio(p, "pkg", "install", "-e", "native")
    assert installed.returncode == 0, installed.stdout
    _commit_and_snapshot(p)
    return p


def _capture(p: Path) -> subprocess.CompletedProcess[str]:
    """pio-lock capture for env native, then commit and refresh the snapshot."""
    cli = [sys.executable, str(REPO_ROOT / "pio_lock.py"), "-d", str(p)]
    captured = _run_cli([*cli, "capture", "-e", "native"])
    assert captured.returncode == 0, captured.stdout
    _commit_and_snapshot(p)
    return captured


# Two lib_deps specs (in real projects often from different ini files) select one copy each,
# so PlatformIO builds and links both. (A bare "1.0.0" in a library.json does not: PlatformIO's
# LDF ignores a version without an operator when picking the copy.)
_BOTH_SPECS = ["piolocktest/dupdemo @ 1.1.0", "piolocktest/dupdemo @ 1.0.0"]
_BOTH_MAIN = (
    'extern "C" int dupdemo_new(void);\nextern "C" int dupdemo_old(void);\n'
    "int main() { return dupdemo_new() + dupdemo_old(); }\n"
)
_LINKED = (
    "linked duplicate copies of 'dupdemo': dupdemo, dupdemo@1.0.0 — both are built "
    "into this image; make the requirements agree"
)


@requires_pio
def test_build_fails_in_release_when_two_copies_are_linked(tmp_path: Path) -> None:
    p = _duplicates_project(tmp_path, _BOTH_SPECS, _BOTH_MAIN)
    _capture(p)

    released = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert released.returncode != 0, released.stdout
    assert f"ERROR: {_LINKED}" in released.stdout
    assert "build stopped" in released.stdout

    warned = run_pio(p, "run", "-e", "native", env={**_RELEASE, "PIO_LOCK_DRIFT": "warn"})
    assert warned.returncode == 0, warned.stdout
    assert f"WARNING: {_LINKED}" in warned.stdout

    # A program that already exists (the warn build above) is still checked on relink.
    (p / "src" / "main.cpp").write_text(_BOTH_MAIN.replace("+ dupdemo_old", "- dupdemo_old"))
    _commit_and_snapshot(p)
    relinked = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert relinked.returncode != 0, relinked.stdout
    assert f"ERROR: {_LINKED}" in relinked.stdout


@requires_pio
def test_linked_copies_fail_release_when_loaded_from_lib_deps(tmp_path: Path) -> None:
    p = _duplicates_project(tmp_path, _BOTH_SPECS, _BOTH_MAIN, via_lib_deps=True)
    _capture(p)

    released = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert released.returncode != 0, released.stdout
    assert f"ERROR: {_LINKED}" in released.stdout


@requires_pio
def test_linked_copies_only_warn_without_a_lockfile(tmp_path: Path) -> None:
    p = _duplicates_project(tmp_path, _BOTH_SPECS, _BOTH_MAIN)
    assert not (p / "pio.lock.json").exists()

    released = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert released.returncode == 0, released.stdout
    assert f"WARNING: {_LINKED}" in released.stdout


@requires_pio
def test_build_passes_when_only_one_installed_copy_is_linked(tmp_path: Path) -> None:
    p = _duplicates_project(
        tmp_path,
        ["piolocktest/dupdemo @ 1.1.0"],
        'extern "C" int dupdemo_new(void);\nint main() { return dupdemo_new(); }\n',
    )
    captured = _capture(p)
    assert "Warning: duplicate copies of 'dupdemo'" in captured.stdout

    released = run_pio(p, "run", "-e", "native", env=_RELEASE)
    assert released.returncode == 0, released.stdout
    assert "WARNING: duplicate copies of 'dupdemo': dupdemo (1.1.0; requested by lib_deps: " in (
        released.stdout
    )
    assert "linked duplicate" not in released.stdout
