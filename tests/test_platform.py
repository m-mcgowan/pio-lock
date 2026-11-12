"""Unit tests for PioAdapter, its fake, and running the CLI under PlatformIO's Python.

No PlatformIO needed: the real adapter is driven through stub modules returned by an
overridden ``_import``; everything else uses ``tests.fakes.FakePioAdapter``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import pio_lock
from pio_lock import (
    PackageLock,
    PioAdapter,
    PioIncompatible,
    PlatformLock,
    PlatformNotInstalled,
)
from tests.fakes import FakePioAdapter

# ── Stub PlatformIO modules ─────────────────────────────────────────────────


class _StubPlatformPackageManager:
    installed: ClassVar[dict[str, Any]] = {}

    def get_package(self, spec: str) -> Any:
        return self.installed.get(spec)


class _StubPlatformBase:
    packages: ClassVar[dict[str, Any]] = {}

    def configure_project_packages(self, env: str, targets: Any = None) -> None:
        pass

    def get_package(self, name: str) -> Any:
        return None


def _stub_modules(**overrides: Any) -> dict[str, Any]:
    modules: dict[str, Any] = {
        "platformio": SimpleNamespace(__version__="6.1.18"),
        "platformio.project.config": SimpleNamespace(
            ProjectConfig=SimpleNamespace(read=None, get=None, items=None, BUILTIN_VARS={})
        ),
        "platformio.package.manager.platform": SimpleNamespace(
            PlatformPackageManager=_StubPlatformPackageManager
        ),
        "platformio.platform.factory": SimpleNamespace(PlatformFactory=SimpleNamespace(new=None)),
        "platformio.platform.base": SimpleNamespace(PlatformBase=_StubPlatformBase),
    }
    modules.update(overrides)
    return modules


class _StubAdapter(PioAdapter):
    """A real PioAdapter whose PlatformIO imports come from a dict of stubs."""

    def __init__(self, modules: dict[str, Any], broken: dict[str, Exception] | None = None):
        self.modules = modules
        self.broken = broken or {}

    def _import(self, module: str) -> Any:
        if module in self.broken:
            raise self.broken[module]
        if module not in self.modules:
            raise ImportError(f"No module named '{module}'")
        return self.modules[module]


# ── probe ────────────────────────────────────────────────────────────────────


def test_probe_reports_call_and_core_version() -> None:
    adapter = _StubAdapter(
        _stub_modules(),
        broken={"platformio.platform.factory": ImportError("No module named 'factory'")},
    )
    with pytest.raises(PioIncompatible) as exc_info:
        adapter.probe()
    exc = exc_info.value
    assert "platformio.platform.factory" in exc.call
    assert exc.core_version == "6.1.18"
    assert str(exc).startswith("PlatformIO Core 6.1.18 is not compatible with pio-lock: ")
    assert "platformio.platform.factory" in str(exc)
    assert "No module named 'factory'" in str(exc)


def test_probe_reports_missing_attribute() -> None:
    modules = _stub_modules()
    modules["platformio.platform.factory"] = SimpleNamespace(PlatformFactory=SimpleNamespace())
    with pytest.raises(PioIncompatible) as exc_info:
        _StubAdapter(modules).probe()
    assert exc_info.value.call == "platformio.platform.factory.PlatformFactory.new"


def test_probe_passes_with_all_internals_present() -> None:
    _StubAdapter(_stub_modules()).probe()


def test_core_version_unknown_without_platformio() -> None:
    assert _StubAdapter({}).core_version() == "unknown"


def test_exception_messages() -> None:
    exc = PioIncompatible("PlatformFactory.new", "6.9.0", "TypeError: boom")
    assert exc.call == "PlatformFactory.new"
    assert exc.core_version == "6.9.0"
    assert str(exc) == (
        "PlatformIO Core 6.9.0 is not compatible with pio-lock: "
        "PlatformFactory.new failed (TypeError: boom)"
    )
    missing = PlatformNotInstalled("esp32")
    assert missing.env == "esp32"
    assert str(missing) == (
        "platform for env 'esp32' is not installed; run: pio pkg install -e esp32"
    )


# ── resolve_platform ─────────────────────────────────────────────────────────


def test_resolve_platform_not_installed(tmp_project: Path) -> None:
    adapter = FakePioAdapter(platforms={})
    with pytest.raises(PlatformNotInstalled) as exc_info:
        adapter.resolve_platform(tmp_project, "testenv")
    assert "pio pkg install -e testenv" in str(exc_info.value)


def test_real_adapter_raises_not_installed_when_package_missing(tmp_path: Path) -> None:
    """The real adapter maps 'no installed package for the spec' to PlatformNotInstalled."""

    class _View:
        def get(self, section: str, option: str, default: Any = None) -> Any:
            return {"platform": "espressif32", "platform_packages": []}.get(option, default)

    class _Adapter(_StubAdapter):
        def user_config(self, project_dir: Path) -> Any:
            return _View()

    _StubPlatformPackageManager.installed = {}
    with pytest.raises(PlatformNotInstalled) as exc_info:
        _Adapter(_stub_modules()).resolve_platform(tmp_path, "esp32")
    assert exc_info.value.env == "esp32"


# ── FakePioAdapter ───────────────────────────────────────────────────────────


def _platform_lock() -> PlatformLock:
    return PlatformLock(
        spec="espressif32",
        platform_packages_spec=[],
        name="espressif32",
        version="6.6.0",
        src_url=None,
        packages=[],
    )


def test_fake_adapter_returns_platform_and_in_use(tmp_project: Path) -> None:
    lock = _platform_lock()
    fake = FakePioAdapter(
        platforms={"testenv": lock}, in_use={"testenv": ("espressif32", "6.5.0")}
    )
    fake.probe()
    assert fake.core_version() == "6.1.18"
    assert fake.resolve_platform(tmp_project, "testenv") == lock
    assert fake.platform_in_use(tmp_project, "testenv") == ("espressif32", "6.5.0")


def test_fake_adapter_incompatible(tmp_project: Path) -> None:
    fake = FakePioAdapter(platforms={"testenv": _platform_lock()}, incompatible="X.y")
    with pytest.raises(PioIncompatible) as exc_info:
        fake.probe()
    assert exc_info.value.call == "X.y"
    with pytest.raises(PioIncompatible):
        fake.resolve_platform(tmp_project, "testenv")


def test_fake_adapter_config_views(tmp_path: Path) -> None:
    (tmp_path / "platformio.ini").write_text(
        "[platformio]\nextra_configs = pio.lock.ini\n"
        "[env]\nplatform = espressif32\n"
        "[env:a]\nplatform_packages =\n  tool-a @ 1.0\n  tool-b @ 2.0\n[env:b]\n"
    )
    (tmp_path / "pio.lock.ini").write_text("[env:a]\nplatform = platformio/espressif32@6.6.0\n")
    fake = FakePioAdapter(platforms={})
    user = fake.user_config(tmp_path)
    full = fake.full_config(tmp_path)
    assert user.get("env:a", "platform") == "espressif32"
    assert user.get("env:b", "platform") == "espressif32"
    assert user.get("env:a", "platform_packages") == ["tool-a @ 1.0", "tool-b @ 2.0"]
    assert user.get("env:b", "platform_packages", []) == []
    assert full.get("env:a", "platform") == "platformio/espressif32@6.6.0"
    assert user.envs() == ["a", "b"]
    assert fake.config_files(tmp_path) == [tmp_path / "platformio.ini", tmp_path / "pio.lock.ini"]


def test_adapter_factory_seam_defaults_to_real_adapter() -> None:
    assert pio_lock._pio_adapter_factory is PioAdapter


# ── Re-exec under PlatformIO's Python ────────────────────────────────────────


@pytest.fixture
def reexec(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the importability check, PIO Python lookup and os.execve."""
    state: dict[str, Any] = {"importable": False, "python": "/pio/penv/bin/python", "exec": None}
    monkeypatch.delenv(pio_lock._REEXEC_ENV, raising=False)
    monkeypatch.setattr(pio_lock, "_platformio_importable", lambda: state["importable"])
    monkeypatch.setattr(pio_lock, "_find_pio_python", lambda: state["python"])

    def fake_execve(path: str, args: list[str], env: dict[str, str]) -> None:
        state["exec"] = (path, args, env)

    monkeypatch.setattr(os, "execve", fake_execve)
    return state


def test_reexec_under_pio_python_when_platformio_missing(reexec: dict[str, Any]) -> None:
    pio_lock._ensure_pio_python(["-d", "proj", "capture", "-e", "native"])
    path, args, env = reexec["exec"]
    assert path == "/pio/penv/bin/python"
    assert args == [
        "/pio/penv/bin/python",
        "-I",
        os.path.abspath(pio_lock.__file__),
        "-d",
        "proj",
        "capture",
        "-e",
        "native",
    ]
    assert env["PIO_LOCK_REEXEC"] == "1"
    assert env["PATH"] == os.environ["PATH"]


def test_no_reexec_when_platformio_importable(reexec: dict[str, Any], capsys) -> None:
    reexec["importable"] = True
    pio_lock._ensure_pio_python(["capture", "-e", "native"])
    assert reexec["exec"] is None
    assert capsys.readouterr().err == ""


def test_no_reexec_when_already_reexeced(
    reexec: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("PIO_LOCK_REEXEC", "1")
    pio_lock._ensure_pio_python(["capture", "-e", "native"])
    assert reexec["exec"] is None
    assert "platform locking disabled" in capsys.readouterr().err


def test_warns_and_continues_when_pio_python_not_found(reexec: dict[str, Any], capsys) -> None:
    reexec["python"] = None
    pio_lock._ensure_pio_python(["capture", "-e", "native"])
    assert reexec["exec"] is None
    assert capsys.readouterr().err.strip() == (
        "pio-lock: PlatformIO's Python not found; platform locking disabled "
        "(install pio-lock into PlatformIO's Python: ~/.platformio/penv/bin/pip install pio-lock)"
    )


def test_find_pio_python_parses_system_info(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    info = {
        "core_version": {"title": "PlatformIO Core", "value": "6.1.18"},
        "python_exe": {"title": "Python Executable", "value": "/pio/penv/bin/python"},
    }

    def fake_run(args: list[str], cwd: Any = None, check: bool = True) -> str:
        calls.append(args)
        return json.dumps(info)

    monkeypatch.setattr(pio_lock, "_run_cmd", fake_run)
    assert pio_lock._find_pio_python() == "/pio/penv/bin/python"
    assert calls == [["pio", "system", "info", "--json-output"]]


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("pio"),
        subprocess.CalledProcessError(1, ["pio"]),
        "not json",
        json.dumps({"core_version": {"value": "6.1.18"}}),
        json.dumps({"python_exe": {"value": ""}}),
    ],
)
def test_find_pio_python_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, outcome: Any
) -> None:
    def fake_run(args: list[str], cwd: Any = None, check: bool = True) -> str:
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)

    monkeypatch.setattr(pio_lock, "_run_cmd", fake_run)
    assert pio_lock._find_pio_python() is None


def test_main_with_explicit_argv_never_reexecs(
    reexec: dict[str, Any], tmp_path: Path, capsys
) -> None:
    """In-process callers pass argv; only a real CLI run may replace the process."""
    pio_lock.main(["-d", str(tmp_path), "snapshot-print"])
    assert reexec["exec"] is None


def test_main_without_argv_reexecs_with_sys_argv(
    reexec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["pio-lock", "-d", "x", "snapshot-print"])

    def stop(path: str, args: list[str], env: dict[str, str]) -> None:
        raise SystemExit(args[3:])

    monkeypatch.setattr(os, "execve", stop)
    with pytest.raises(SystemExit) as exc_info:
        pio_lock.main()
    assert exc_info.value.code == ["-d", "x", "snapshot-print"]


def test_capture_helpers_tolerate_missing_pio_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_project: Path
) -> None:
    """Without the pio CLI (warn-and-continue path) capture's pio lookups degrade to 'unknown'."""

    def no_pio(args: list[str], cwd: Any = None, check: bool = True) -> str:
        raise FileNotFoundError(2, "No such file or directory", "pio")

    monkeypatch.setattr(pio_lock, "_run_cmd", no_pio)
    assert pio_lock.get_pio_core_version() == "unknown"
    assert pio_lock.get_platform_url(tmp_project, "testenv") == "unknown"


def test_reexeced_process_clears_marker(
    reexec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once running under PIO's Python, child processes (e.g. a nested pio-lock) start clean."""
    monkeypatch.setenv("PIO_LOCK_REEXEC", "1")
    reexec["importable"] = True
    pio_lock._ensure_pio_python(["capture", "-e", "native"])
    assert "PIO_LOCK_REEXEC" not in os.environ
    assert reexec["exec"] is None


def test_config_view_expands_project_dir_from_project(tmp_path: Path) -> None:
    """The real adapter pins PROJECT_DIR/PROJECT_HASH on the view instead of os.getcwd()."""
    created: list[Any] = []

    class _Config:
        BUILTIN_VARS: ClassVar[dict[str, Any]] = {"PROJECT_DIR": os.getcwd, "UNIX_TIME": str}

        def __init__(self, path: str, parse_extra: bool = True) -> None:
            created.append(self)

        def get(self, section: str, option: str, default: Any = None) -> Any:
            return default

    modules = _stub_modules()
    modules["platformio.project.config"] = SimpleNamespace(ProjectConfig=_Config)
    _StubAdapter(modules).user_config(tmp_path)
    builtins = created[0].BUILTIN_VARS
    assert builtins["PROJECT_DIR"]() == str(tmp_path.resolve())
    assert builtins["PROJECT_HASH"]().startswith(tmp_path.resolve().name + "-")
    assert builtins["UNIX_TIME"] is str
    assert _Config.BUILTIN_VARS["PROJECT_DIR"] is os.getcwd


# ── capture: lockfile v2 and pio.lock.ini ────────────────────────────────────

_PIOARDUINO_URL = (
    "https://github.com/pioarduino/platform-espressif32/releases/download/54.03.20/"
    "platform-espressif32.zip"
)


def _lock(
    *,
    spec: str = "espressif32",
    name: str = "espressif32",
    version: str = "6.6.0",
    src_url: str | None = None,
    packages: list[PackageLock] | None = None,
    platform_packages_spec: list[str] | None = None,
) -> PlatformLock:
    return PlatformLock(
        spec=spec,
        platform_packages_spec=platform_packages_spec or [],
        name=name,
        version=version,
        src_url=src_url,
        packages=packages or [],
    )


def _read_lock(project: Path) -> dict[str, Any]:
    return dict(json.loads((project / "pio.lock.json").read_text()))


def test_capture_writes_v2_with_platform(tmp_project: Path, fake_pio: Any) -> None:
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    data = _read_lock(tmp_project)
    assert data["lockfile_version"] == 2
    assert "global_packages" not in data
    assert "platform_url" not in data
    assert data["pio_core_version"] == "6.1.18"
    assert data["envs"]["testenv"]["platform"]["version"] == "6.6.0"
    assert data["envs"]["testenv"]["platform"]["spec"] == "espressif32"
    assert data["envs"]["testenv"]["libraries"] == []


def test_capture_writes_lock_ini(tmp_project: Path, fake_pio: Any) -> None:
    fake_pio(
        {
            "testenv": _lock(
                packages=[
                    PackageLock("toolchain-xtensa-esp32", "8.4.0", owner="espressif"),
                    PackageLock("tool-esptoolpy", "1.40501.0", owner="platformio"),
                ]
            )
        }
    )
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    ini = (tmp_project / "pio.lock.ini").read_text()
    assert "[env:testenv]" in ini
    assert "platform = platformio/espressif32@6.6.0" in ini
    assert "  espressif/toolchain-xtensa-esp32@8.4.0\n" in ini
    assert "  platformio/tool-esptoolpy@1.40501.0\n" in ini
    assert ini.startswith("#")
    assert "extra_configs" in ini.split("[env:")[0]


def test_capture_writes_lock_ini_next_to_custom_output(tmp_project: Path, fake_pio: Any) -> None:
    fake_pio({"testenv": _lock()})
    output = tmp_project / "custom" / "lock.json"
    assert pio_lock.capture(tmp_project, ["testenv"], output_path=output) == 0
    assert (tmp_project / "custom" / "pio.lock.ini").exists()
    assert not (tmp_project / "pio.lock.ini").exists()


def test_lock_ini_uses_src_url_for_url_platforms() -> None:
    lockdata = {
        "envs": {
            "esp32": {
                "platform": asdict(
                    _lock(spec=_PIOARDUINO_URL, version="54.3.20", src_url=_PIOARDUINO_URL)
                ),
                "libraries": [],
            }
        }
    }
    ini = pio_lock.render_lock_ini(lockdata)
    assert f"platform = {_PIOARDUINO_URL}\n" in ini


def test_lock_ini_uses_platformio_owner_when_unknown() -> None:
    lockdata = {"envs": {"e": {"platform": asdict(_lock()), "libraries": []}}}
    assert "platform = platformio/espressif32@6.6.0\n" in pio_lock.render_lock_ini(lockdata)


def test_lock_ini_keeps_user_platform_packages(tmp_project: Path, fake_pio: Any) -> None:
    user_pkg = (
        "framework-arduinoespressif32 @ https://github.com/espressif/arduino-esp32.git#abc123"
    )
    (tmp_project / "platformio.ini").write_text(
        f"[env:testenv]\nplatform = espressif32\nplatform_packages =\n  {user_pkg}\n"
    )
    git_url = "https://github.com/espressif/arduino-esp32.git#abc123"
    fake_pio(
        {
            "testenv": _lock(
                platform_packages_spec=[user_pkg],
                packages=[
                    PackageLock(
                        "framework-arduinoespressif32", "3.0.0", owner=None, src_url=git_url
                    ),
                    PackageLock("toolchain-xtensa-esp32", "8.4.0", owner="espressif"),
                ],
            )
        }
    )
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    platform = _read_lock(tmp_project)["envs"]["testenv"]["platform"]
    assert platform["platform_packages_spec"] == [user_pkg]
    assert platform["packages"][0]["src_url"] == git_url
    ini = (tmp_project / "pio.lock.ini").read_text()
    assert f"  {user_pkg}\n" in ini


def test_lock_ini_omits_local_file_packages() -> None:
    lockdata = {
        "envs": {
            "e": {
                "platform": asdict(
                    _lock(
                        packages=[
                            PackageLock("tool-esptoolpy", "5.0.0", src_url="file:///Users/x/tool"),
                            PackageLock("toolchain-xtensa-esp32", "8.4.0", owner="espressif"),
                        ]
                    )
                ),
                "libraries": [],
            }
        }
    }
    ini = pio_lock.render_lock_ini(lockdata)
    assert "file://" not in ini
    assert "tool-esptoolpy" not in ini
    assert "  espressif/toolchain-xtensa-esp32@8.4.0\n" in ini


def test_partial_capture_keeps_other_envs(tmp_project: Path, fake_pio: Any) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32\n[env:a]\n[env:b]\n"
    )
    fake_pio({"a": _lock(), "b": _lock(version="6.5.0")})
    assert pio_lock.capture(tmp_project, ["a", "b"]) == 0
    fake_pio({"a": _lock(version="6.7.0")})
    assert pio_lock.capture(tmp_project, ["a"]) == 0
    envs = _read_lock(tmp_project)["envs"]
    assert envs["a"]["platform"]["version"] == "6.7.0"
    assert envs["b"]["platform"]["version"] == "6.5.0"
    ini = (tmp_project / "pio.lock.ini").read_text()
    assert "[env:a]" in ini
    assert "[env:b]" in ini


def test_capture_prunes_removed_envs(tmp_project: Path, fake_pio: Any, capsys: Any) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32\n[env:old]\n[env:testenv]\n"
    )
    fake_pio({"old": _lock(), "testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["old", "testenv"]) == 0
    (tmp_project / "platformio.ini").write_text("[env:testenv]\nplatform = espressif32\n")
    capsys.readouterr()
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    assert "old" not in _read_lock(tmp_project)["envs"]
    assert "[env:old]" not in (tmp_project / "pio.lock.ini").read_text()
    assert "Pruned env 'old'" in capsys.readouterr().out


def test_capture_excludes_pio_lock_itself(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any
) -> None:
    make_registry_lib("testenv", "pio-lock", "0.4.0", owner="m-mcgowan", project_dir=tmp_project)
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    names = [lib["name"] for lib in _read_lock(tmp_project)["envs"]["testenv"]["libraries"]]
    assert names == ["MyLib"]


def test_capture_platform_inherited_from_env_section(tmp_project: Path, fake_pio: Any) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32\n[env:a]\nboard = x\n[env:b]\nboard = y\n"
    )
    # The fake resolves spec/platform_packages_spec from the user's config view, like the real one.
    fake_pio({"a": _lock(spec="?"), "b": _lock(spec="?")})
    assert pio_lock.capture(tmp_project, ["a", "b"]) == 0
    envs = _read_lock(tmp_project)["envs"]
    assert envs["a"]["platform"]["spec"] == "espressif32"
    assert envs["b"]["platform"]["spec"] == "espressif32"


def test_capture_incompatible_pio_keeps_libraries(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any, capsys: Any
) -> None:
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    fake_pio({"testenv": _lock()}, incompatible="PlatformFactory.from_env")
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    env_lock = _read_lock(tmp_project)["envs"]["testenv"]
    assert "platform" not in env_lock
    assert [lib["name"] for lib in env_lock["libraries"]] == ["MyLib"]
    out = capsys.readouterr()
    assert "PlatformFactory.from_env" in out.out + out.err
    assert not (tmp_project / "pio.lock.ini").read_text().count("[env:")


def test_capture_platform_not_installed_fails(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    fake_pio({})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 1
    assert not (tmp_project / "pio.lock.json").exists()
    assert not (tmp_project / "pio.lock.ini").exists()
    out = capsys.readouterr()
    assert "pio pkg install -e testenv" in out.out + out.err


def test_load_lockfile_defaults_v1(tmp_path: Path) -> None:
    path = tmp_path / "pio.lock.json"
    path.write_text(json.dumps({"envs": {"a": {"libraries": []}}}))
    assert pio_lock.load_lockfile(path)["lockfile_version"] == 1
    path.write_text(json.dumps({"lockfile_version": 2, "envs": {}}))
    assert pio_lock.load_lockfile(path)["lockfile_version"] == 2


def test_env_lock_is_stale() -> None:
    env_lock = {"platform": asdict(_lock(platform_packages_spec=["tool-a @ 1.0"]))}
    assert not pio_lock.env_lock_is_stale(env_lock, "espressif32", ["tool-a @ 1.0"])
    assert pio_lock.env_lock_is_stale(env_lock, "espressif32@6.7.0", ["tool-a @ 1.0"])
    assert pio_lock.env_lock_is_stale(env_lock, "espressif32", [])
    assert pio_lock.env_lock_is_stale({"libraries": []}, "espressif32", [])
