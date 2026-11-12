"""Unit tests for PioAdapter, its fake, and running the CLI under PlatformIO's Python.

No PlatformIO needed: the real adapter is driven through stub modules returned by an
overridden ``_import``; everything else uses ``tests.fakes.FakePioAdapter``.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from dataclasses import asdict
from dataclasses import replace as dataclasses_replace
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
from tests.conftest import write_lockfile
from tests.fakes import FakePioAdapter

# ── Stub PlatformIO modules ─────────────────────────────────────────────────


class _StubPackageSpec:
    def __init__(self, owner: Any = None, name: Any = None) -> None:
        self.owner, self.name = owner, name


class _StubPlatformPackageManager:
    installed: ClassVar[dict[str, Any]] = {}

    registry: ClassVar[Any] = {"versions": [{"name": "6.9.0"}, {"name": "6.6.0"}]}

    def get_package(self, spec: str) -> Any:
        return self.installed.get(spec)

    def install(self, spec: Any, skip_dependencies: bool = False, **kwargs: Any) -> Any:
        raise NotImplementedError

    def fetch_registry_package(self, spec: Any) -> Any:
        if isinstance(self.registry, Exception):
            raise self.registry
        return self.registry

    def pick_best_registry_version(self, versions: Any, spec: Any = None) -> Any:
        return versions[0] if versions else None


class _StubToolPackageManager:
    installed: ClassVar[list[Any]] = []

    def get_package(self, spec: str) -> Any:
        return None

    def get_installed(self) -> list[Any]:
        return self.installed


class _StubPlatformBase:
    packages: ClassVar[dict[str, Any]] = {}

    def configure_project_packages(self, env: str, targets: Any = None) -> None:
        pass

    def get_package(self, name: str) -> Any:
        return None

    def install_required_packages(self, force: bool = False) -> None:
        pass


def _stub_modules(**overrides: Any) -> dict[str, Any]:
    modules: dict[str, Any] = {
        "platformio": SimpleNamespace(__version__="6.1.18"),
        "platformio.project.config": SimpleNamespace(
            ProjectConfig=SimpleNamespace(read=None, get=None, items=None, BUILTIN_VARS={})
        ),
        "platformio.package.manager.platform": SimpleNamespace(
            PlatformPackageManager=_StubPlatformPackageManager
        ),
        "platformio.package.manager.tool": SimpleNamespace(
            ToolPackageManager=_StubToolPackageManager
        ),
        "platformio.package.meta": SimpleNamespace(PackageSpec=_StubPackageSpec),
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


def test_latest_platform_version_picks_best_registry_version() -> None:
    adapter = _StubAdapter(_stub_modules())
    assert adapter.latest_platform_version("espressif32", "platformio") == "6.9.0"


def test_latest_platform_version_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_StubPlatformPackageManager, "registry", OSError("offline"))
    assert _StubAdapter(_stub_modules()).latest_platform_version("espressif32", None) is None


def test_latest_platform_version_incompatible_when_api_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_StubPlatformPackageManager, "registry", TypeError("bad signature"))
    with pytest.raises(PioIncompatible):
        _StubAdapter(_stub_modules()).latest_platform_version("espressif32", None)


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
        "platform for env 'esp32' is not installed; run: pio-lock capture --install -e esp32"
    )


# ── resolve_platform ─────────────────────────────────────────────────────────


def test_resolve_platform_not_installed(tmp_project: Path) -> None:
    adapter = FakePioAdapter(platforms={})
    with pytest.raises(PlatformNotInstalled) as exc_info:
        adapter.resolve_platform(tmp_project, "testenv")
    assert "pio-lock capture --install -e testenv" in str(exc_info.value)


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
    # PlatformIO records the git source in its normalized git+ form.
    git_url = "git+https://github.com/espressif/arduino-esp32.git#abc123"
    fake_pio(
        {
            "testenv": _lock(
                platform_packages_spec=[user_pkg],
                packages=[
                    PackageLock(
                        "framework-arduinoespressif32",
                        "3.0.0+sha.abc123",
                        owner=None,
                        src_url=git_url,
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
    assert (
        "  framework-arduinoespressif32 @ "
        "git+https://github.com/espressif/arduino-esp32.git#abc123\n"
    ) in ini


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
    assert "pio-lock capture --install -e testenv" in out.out + out.err


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


# ── capture fix round 1 ──────────────────────────────────────────────────────


def test_capture_unknown_env_fails_cleanly(tmp_project: Path, fake_pio: Any, capsys: Any) -> None:
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv", "nope"]) == 1
    assert "Error: env 'nope' not found in platformio.ini" in capsys.readouterr().err
    assert not (tmp_project / "pio.lock.json").exists()
    assert not (tmp_project / "pio.lock.ini").exists()


def test_capture_env_without_platform_fails_cleanly(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    (tmp_project / "platformio.ini").write_text("[env:testenv]\nboard = esp32dev\n")
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 1
    assert "Error: env 'testenv' has no platform in platformio.ini" in capsys.readouterr().err
    assert not (tmp_project / "pio.lock.json").exists()


def test_fake_adapter_raises_like_real_for_env_without_platform(tmp_project: Path) -> None:
    fake = FakePioAdapter(platforms={"nope": _lock()})
    with pytest.raises(ValueError, match="env 'nope' has no platform"):
        fake.resolve_platform(tmp_project, "nope")


_GIT_PLATFORM = "git+https://github.com/platformio/platform-espressif32.git"


def _ini_for(platform: PlatformLock) -> str:
    return pio_lock.render_lock_ini({"envs": {"e": {"platform": asdict(platform)}}})


def test_lock_ini_pins_git_platform_to_resolved_sha() -> None:
    ini = _ini_for(
        _lock(
            spec=f"{_GIT_PLATFORM}#main",
            version="6.6.0+sha.1a2b3c4",
            src_url=f"{_GIT_PLATFORM}#main",
        )
    )
    assert f"platform = {_GIT_PLATFORM}#1a2b3c4\n" in ini


def test_lock_ini_pins_unpinned_git_platform() -> None:
    ini = _ini_for(_lock(spec=_GIT_PLATFORM, version="6.6.0+sha.1a2b3c4", src_url=_GIT_PLATFORM))
    assert f"platform = {_GIT_PLATFORM}#1a2b3c4\n" in ini


def test_lock_ini_pins_git_package_to_resolved_sha() -> None:
    url = "git+https://github.com/espressif/arduino-esp32.git#master"
    ini = _ini_for(
        _lock(
            packages=[
                PackageLock("framework-arduinoespressif32", "3.0.0+sha.9f8e7d6", src_url=url)
            ]
        )
    )
    assert (
        "  framework-arduinoespressif32 @ "
        "git+https://github.com/espressif/arduino-esp32.git#9f8e7d6\n"
    ) in ini


def test_lock_ini_keeps_git_url_without_sha_and_warns(capsys: Any) -> None:
    ini = _ini_for(_lock(spec=_GIT_PLATFORM, version="6.6.0", src_url=_GIT_PLATFORM))
    assert f"platform = {_GIT_PLATFORM}\n" in ini
    assert _GIT_PLATFORM in capsys.readouterr().err


def test_lock_ini_leaves_release_zip_url_alone() -> None:
    ini = _ini_for(
        _lock(spec=_PIOARDUINO_URL, version="54.3.20+sha.abc1234", src_url=_PIOARDUINO_URL)
    )
    assert f"platform = {_PIOARDUINO_URL}\n" in ini


def test_lock_ini_uses_platform_owner() -> None:
    lock = _lock(name="myplatform", version="1.2.3")
    lock.owner = "acme"
    assert "platform = acme/myplatform@1.2.3\n" in _ini_for(lock)


def test_capture_records_platform_owner(tmp_project: Path, fake_pio: Any) -> None:
    lock = _lock()
    lock.owner = "acme"
    fake_pio({"testenv": lock})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["owner"] == "acme"


def test_capture_incompatible_keeps_previous_platform(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any, capsys: Any
) -> None:
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    fake_pio({"testenv": _lock()}, incompatible="PlatformFactory.new")
    capsys.readouterr()
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    env_lock = _read_lock(tmp_project)["envs"]["testenv"]
    assert env_lock["platform"]["version"] == "6.6.0"
    assert [lib["name"] for lib in env_lock["libraries"]] == ["MyLib"]
    assert (
        "platform for env 'testenv' not re-captured (PlatformIO incompatible); "
        "keeping the previous lock"
    ) in capsys.readouterr().err
    assert "[env:testenv]" in (tmp_project / "pio.lock.ini").read_text()


def test_capture_upgrades_v1_lockfile(tmp_project: Path, fake_pio: Any) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32\n[env:testenv]\n[env:legacy]\n"
    )
    write_lockfile(
        tmp_project,
        {"legacy": [{"name": "Old", "type": "registry", "version": "1.0.0", "owner": "o"}]},
    )
    fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    data = _read_lock(tmp_project)
    assert data["lockfile_version"] == 2
    assert "global_packages" not in data
    assert "platform_url" not in data
    assert data["envs"]["legacy"] == {
        "libraries": [{"name": "Old", "type": "registry", "version": "1.0.0", "owner": "o"}]
    }
    assert "platform" in data["envs"]["testenv"]


# ── check / restore of the platform ─────────────────────────────────────────


_INCLUDE = "[platformio]\nextra_configs = pio.lock.ini\n"


def _locked_project(project: Path, fake_pio: Any, lock: PlatformLock) -> None:
    """Capture ``lock`` for ``testenv`` (so the lockfile is v2) with pio.lock.ini included."""
    ini = project / "platformio.ini"
    ini.write_text(_INCLUDE + ini.read_text())
    fake_pio({"testenv": lock})
    assert pio_lock.capture(project, ["testenv"]) == 0


def test_check_passes_for_matching_platform(tmp_project: Path, fake_pio: Any, capsys: Any) -> None:
    _locked_project(tmp_project, fake_pio, _lock())
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    assert "DRIFT" not in capsys.readouterr().out


def test_check_reports_platform_version_drift(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    _locked_project(tmp_project, fake_pio, _lock(version="6.6.0"))
    fake_pio({"testenv": _lock(version="6.6.0")}, in_use={"testenv": ("espressif32", "6.7.0")})
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "DRIFT:" in out
    assert "locked 6.6.0, in use 6.7.0" in out


def test_check_reports_stale_platform_spec(tmp_project: Path, fake_pio: Any, capsys: Any) -> None:
    _locked_project(tmp_project, fake_pio, _lock())
    (tmp_project / "platformio.ini").write_text(
        _INCLUDE + "[env:testenv]\nplatform = espressif32@^7\n"
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "platform spec changed: 'espressif32' → 'espressif32@^7'; run capture --install" in out


def test_check_reports_stale_platform_packages(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    _locked_project(tmp_project, fake_pio, _lock())
    (tmp_project / "platformio.ini").write_text(
        _INCLUDE + "[env:testenv]\nplatform = espressif32\nplatform_packages =\n  tool-foo@1.2.3\n"
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "DRIFT:" in out
    assert "platform_packages changed" in out
    assert "tool-foo@1.2.3" in out
    assert "run capture --install" in out


def test_check_reports_package_drift(tmp_project: Path, fake_pio: Any, capsys: Any) -> None:
    locked = _lock(
        packages=[
            PackageLock("toolchain-xtensa", "8.4.0", owner="espressif"),
            PackageLock("tool-local", "1.0.0", src_url="file:///opt/tool-local"),
            PackageLock("tool-gone", "2.0.0", owner="espressif"),
        ]
    )
    _locked_project(tmp_project, fake_pio, locked)
    # The user view resolves newer packages, but only these exact versions are installed.
    fake_pio(
        {"testenv": locked},
        installed_packages={("toolchain-xtensa", "8.5.0"), ("tool-local", "1.0.0")},
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "package toolchain-xtensa: locked 8.4.0 is not installed" in out
    assert "package tool-gone: locked 2.0.0 is not installed" in out
    assert "tool-local" not in out


def test_check_ignores_newer_installed_package_when_locked_one_is_installed(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    locked = _lock(packages=[PackageLock("toolchain-xtensa", "8.4.0", owner="espressif")])
    _locked_project(tmp_project, fake_pio, locked)
    # What the user's view resolves (highest installed) is newer than the lock.
    newer = _lock(packages=[PackageLock("toolchain-xtensa", "8.4.1", owner="espressif")])
    fake_pio(
        {"testenv": newer},
        in_use={"testenv": ("espressif32", "6.6.0")},
        installed_packages={("toolchain-xtensa", "8.4.0"), ("toolchain-xtensa", "8.4.1")},
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    assert "DRIFT" not in capsys.readouterr().out


def test_check_platform_not_installed_is_drift(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    _locked_project(tmp_project, fake_pio, _lock())
    fake_pio({})
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    assert "DRIFT:" in capsys.readouterr().out


def test_check_incompatible_pio_warns_and_checks_libraries(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any, capsys: Any
) -> None:
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    _locked_project(tmp_project, fake_pio, _lock())
    fake_pio({"testenv": _lock()}, incompatible="platformio.x.y")
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    captured = capsys.readouterr()
    assert "WARNING:" in captured.out
    assert "DRIFT" not in captured.out


def test_check_incompatible_pio_still_fails_on_library_drift(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any
) -> None:
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    _locked_project(tmp_project, fake_pio, _lock())
    make_registry_lib("testenv", "MyLib", "1.1.0", project_dir=tmp_project)
    fake_pio({"testenv": _lock()}, incompatible="platformio.x.y")
    assert pio_lock.check(tmp_project, ["testenv"]) == 1


def test_check_v1_lockfile_skips_platform(tmp_project: Path, capsys: Any, make_registry_lib: Any):
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    write_lockfile(
        tmp_project,
        {"testenv": [{"name": "MyLib", "type": "registry", "version": "1.0.0", "owner": "o"}]},
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    assert (
        "platform not checked: lockfile is v1; run capture to lock the platform"
        in capsys.readouterr().out
    )


def _drifted(tmp_project: Path, fake_pio: Any, make_registry_lib: Any) -> None:
    """Platform drift (6.6.0 locked, 6.7.0 in use) and library drift."""
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    _locked_project(tmp_project, fake_pio, _lock())
    make_registry_lib("testenv", "MyLib", "1.1.0", project_dir=tmp_project)
    fake_pio({"testenv": _lock()}, in_use={"testenv": ("espressif32", "6.7.0")})


def test_check_warn_only_exits_zero_with_warnings(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any, capsys: Any
) -> None:
    _drifted(tmp_project, fake_pio, make_registry_lib)
    assert pio_lock.check(tmp_project, ["testenv"], warn_only=True) == 0
    out = capsys.readouterr().out
    assert "WARNING:" in out
    assert "locked 6.6.0, in use 6.7.0" in out
    assert "DRIFT:" not in out


def test_check_drift_env_var_warn_equals_flag(
    tmp_project: Path,
    fake_pio: Any,
    make_registry_lib: Any,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drifted(tmp_project, fake_pio, make_registry_lib)
    monkeypatch.setenv("PIO_LOCK_DRIFT", "warn")
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    out = capsys.readouterr().out
    assert "WARNING:" in out
    assert "DRIFT:" not in out


def test_check_drift_env_var_fail_does_not_override_flag(
    tmp_project: Path,
    fake_pio: Any,
    make_registry_lib: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drifted(tmp_project, fake_pio, make_registry_lib)
    monkeypatch.setenv("PIO_LOCK_DRIFT", "fail")
    assert pio_lock.check(tmp_project, ["testenv"], warn_only=True) == 0
    assert pio_lock.check(tmp_project, ["testenv"]) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("warn", "warn"), (" FAIL ", "fail"), ("bogus", None), ("", None), (None, None)],
)
def test_drift_policy_from_env(monkeypatch: pytest.MonkeyPatch, value: Any, expected: Any) -> None:
    if value is None:
        monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    else:
        monkeypatch.setenv("PIO_LOCK_DRIFT", value)
    assert pio_lock._drift_policy_from_env() == expected


def test_cli_check_warn_only_flag(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any
) -> None:
    _drifted(tmp_project, fake_pio, make_registry_lib)
    argv = ["-d", str(tmp_project), "check", "-e", "testenv"]
    assert pio_lock.main(argv) == 1
    assert pio_lock.main([*argv, "--warn-only"]) == 0


def _install_calls(log: list[list[str]]) -> list[list[str]]:
    return [c for c in log if c[:3] == ["pio", "pkg", "install"]]


def test_restore_installs_platform_before_libraries(
    tmp_project: Path, fake_pio: Any, mock_git_commands: Any, capsys: Any
) -> None:
    locked = _lock(
        packages=[
            PackageLock("toolchain-xtensa", "8.4.0", owner="espressif"),
            PackageLock("tool-local", "1.0.0", src_url="file:///opt/tool-local"),
        ]
    )
    fake_pio({"testenv": locked})
    (tmp_project / ".pio" / "libdeps" / "testenv").mkdir(parents=True)
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    data = _read_lock(tmp_project)
    data["envs"]["testenv"]["libraries"] = [
        {"name": "NewLib", "type": "registry", "version": "3.0.0", "owner": "acme"}
    ]
    (tmp_project / "pio.lock.json").write_text(json.dumps(data))
    mock_git_commands.clear()

    assert pio_lock.restore(tmp_project, ["testenv"]) == 0
    calls = _install_calls(mock_git_commands)
    assert len(calls) == 3
    first = calls[0]
    assert first[first.index("--platform") + 1] == "platformio/espressif32@6.6.0"
    assert "--no-save" in first
    assert first[first.index("-e") + 1] == "testenv"
    second = calls[1]
    assert second[second.index("--tool") + 1] == "espressif/toolchain-xtensa@8.4.0"
    assert "--no-save" in second
    assert "--library" in calls[2]
    assert "managed by the platform" in capsys.readouterr().out
    assert not any("tool-local" in " ".join(c) for c in calls)


def test_restore_v1_lockfile_installs_libraries_only(
    tmp_project: Path, mock_git_commands: Any
) -> None:
    (tmp_project / ".pio" / "libdeps" / "testenv").mkdir(parents=True)
    write_lockfile(
        tmp_project,
        {"testenv": [{"name": "NewLib", "type": "registry", "version": "3.0.0", "owner": "a"}]},
    )
    assert pio_lock.restore(tmp_project, ["testenv"]) == 0
    calls = _install_calls(mock_git_commands)
    assert len(calls) == 1
    assert "--platform" not in calls[0]


def _tool(name: str, version: str) -> Any:
    return SimpleNamespace(metadata=SimpleNamespace(name=name, version=version))


def test_real_adapter_package_installed_matches_exact_version(monkeypatch: Any) -> None:
    monkeypatch.setattr(_StubToolPackageManager, "installed", [_tool("tool-a", "1.0.1")])
    adapter = _StubAdapter(_stub_modules())
    assert not adapter.package_installed({"name": "tool-a", "version": "1.0.0", "owner": "o"})
    monkeypatch.setattr(
        _StubToolPackageManager, "installed", [_tool("tool-a", "1.0.1"), _tool("tool-a", "1.0.0")]
    )
    adapter = _StubAdapter(_stub_modules())  # a new adapter: the installed list is cached
    assert adapter.package_installed({"name": "tool-a", "version": "1.0.0", "owner": "o"})
    # file:// packages have no exact spec: matched by name and version.
    assert adapter.package_installed(
        {"name": "tool-a", "version": "1.0.1", "src_url": "file:///opt/a"}
    )


def test_real_adapter_package_installed_incompatible_api() -> None:
    adapter = _StubAdapter(
        _stub_modules(), broken={"platformio.package.manager.tool": ImportError("x")}
    )
    with pytest.raises(PioIncompatible):
        adapter.package_installed({"name": "tool-a", "version": "1.0.0"})


def test_check_config_error_is_clean_error(
    tmp_project: Path, fake_pio: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    adapter = fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0

    def broken(project_dir: Path) -> Any:
        raise pio_lock.ProjectConfigError("invalid PlatformIO project configuration in x")

    monkeypatch.setattr(adapter, "user_config", broken)
    monkeypatch.setattr(adapter, "config_files", broken)
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    assert "Error: invalid PlatformIO project configuration" in capsys.readouterr().err


class InvalidProjectConfError(Exception):
    """Stands in for platformio.project.exception.InvalidProjectConfError (matched by name)."""


@pytest.mark.parametrize("exc", [InvalidProjectConfError("bad"), configparser.Error("bad")])
def test_real_adapter_converts_config_parse_errors(tmp_path: Path, exc: Exception) -> None:
    class _Config:
        BUILTIN_VARS: ClassVar[dict[str, Any]] = {}

        def __init__(self, path: str, parse_extra: bool = True) -> None:
            raise exc

    modules = _stub_modules(
        **{"platformio.project.config": SimpleNamespace(ProjectConfig=_Config)}
    )
    (tmp_path / "platformio.ini").write_text("[env:x\n")
    with pytest.raises(pio_lock.ProjectConfigError, match=r"platformio\.ini"):
        _StubAdapter(modules).user_config(tmp_path)


# ── check: a config file read after pio.lock.ini overriding the lock ─────────


def _override_after_lock_ini(project: Path) -> None:
    (project / "late.ini").write_text("[env:testenv]\nplatform = espressif32\n")
    (project / "platformio.ini").write_text(
        "[platformio]\nextra_configs = pio.lock.ini\n  late.ini\n"
        "[env:testenv]\nplatform = espressif32\n"
    )


def test_check_reports_override_after_lock_ini(
    tmp_project: Path, fake_pio: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIO_LOCK_ALLOW_OVERRIDE", raising=False)
    _locked_project(tmp_project, fake_pio, _lock())
    _override_after_lock_ini(tmp_project)
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    assert (
        "DRIFT: late.ini sets platform for env 'testenv' after pio.lock.ini"
        in capsys.readouterr().out
    )


def test_check_override_allowed_by_env_var_warns(
    tmp_project: Path, fake_pio: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _locked_project(tmp_project, fake_pio, _lock())
    _override_after_lock_ini(tmp_project)
    monkeypatch.setenv("PIO_LOCK_ALLOW_OVERRIDE", "1")
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    out = capsys.readouterr().out
    assert "WARNING: late.ini sets platform for env 'testenv' after pio.lock.ini" in out
    assert "DRIFT" not in out


def test_lock_overrides_none_without_include(tmp_project: Path, fake_pio: Any) -> None:
    adapter = fake_pio({"testenv": _lock()})
    (tmp_project / "late.ini").write_text("[env:testenv]\nplatform = espressif32@7\n")
    (tmp_project / "platformio.ini").write_text(
        "[platformio]\nextra_configs = late.ini\n[env:testenv]\nplatform = espressif32\n"
    )
    assert pio_lock.lock_overrides(tmp_project, "testenv", adapter) == []


# ── update: moving each env's platform ───────────────────────────────────────


@pytest.fixture
def platform_update(monkeypatch: pytest.MonkeyPatch, mock_git_commands: Any, fake_pio: Any):
    """update with no lib_deps and no GitHub; returns (install the fake, pio commands run)."""
    config = SimpleNamespace(
        get=lambda section, option, default=None: [] if option == "lib_deps" else default,
        _parsed=[],
    )
    config_cls = SimpleNamespace(get_instance=lambda path=None: config)
    monkeypatch.setattr(pio_lock, "_pio_import_fn", lambda: (lambda d: None, None, config_cls))
    monkeypatch.setattr(pio_lock, "_create_github_client_fn", lambda: None)

    def pio_commands() -> list[list[str]]:
        return [args for args in mock_git_commands if args[:1] == ["pio"]]

    return fake_pio, pio_commands


def _move_to(fake: FakePioAdapter, version: str) -> None:
    """The registry offers ``version``; installing it makes it resolvable."""
    fake.latest_versions = {"espressif32": version}
    fake.by_spec = {f"platformio/espressif32@{version}": _lock(version=version)}


def test_update_bare_platform_changes_lock_only(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    ini = (tmp_project / "platformio.ini").read_text()
    fake = install({"testenv": dataclasses_replace(_lock(), owner="platformio")})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    capsys.readouterr()

    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == ini
    platform = _read_lock(tmp_project)["envs"]["testenv"]["platform"]
    assert platform["version"] == "6.7.0"
    assert platform["spec"] == "espressif32"
    assert "platformio/espressif32@6.7.0" in (tmp_project / "pio.lock.ini").read_text()
    assert fake.installed_calls == [("testenv", "platformio/espressif32@6.7.0")]
    out = capsys.readouterr().out
    assert "espressif32: 6.6.0 -> 6.7.0" in out
    assert "edited" not in out
    assert "platformio.ini changed" not in out


def test_update_exact_platform_edits_ini(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    (tmp_project / "platformio.ini").write_text(
        "[env:testenv]\nplatform = espressif32@6.6.0\nboard = esp32dev\n"
    )
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    capsys.readouterr()

    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == (
        "[env:testenv]\nplatform = espressif32@6.7.0\nboard = esp32dev\n"
    )
    platform = _read_lock(tmp_project)["envs"]["testenv"]["platform"]
    assert (platform["spec"], platform["version"]) == ("espressif32@6.7.0", "6.7.0")
    out = capsys.readouterr().out
    assert "espressif32: 6.6.0 -> 6.7.0" in out
    assert "    edited platformio.ini: espressif32@6.6.0 => espressif32@6.7.0" in out
    assert "platformio.ini changed — review and commit it with pio.lock.json" in out


def test_update_exact_platform_lock_only_installs_without_saving(
    tmp_project: Path, platform_update: Any
) -> None:
    install, _ = platform_update
    ini = "[env:testenv]\nplatform = espressif32@6.6.0\nboard = esp32dev\n"
    (tmp_project / "platformio.ini").write_text(ini)
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")

    assert pio_lock.update(tmp_project, ["testenv"], lock_only=True) == 0
    assert (tmp_project / "platformio.ini").read_text() == ini
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.7.0"
    assert fake.resolved_with["testenv"] == "platformio/espressif32@6.7.0"


def test_update_platform_beyond_range_requires_flag(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, pio_commands = platform_update
    ini = "[env:testenv]\nplatform = espressif32@^6.6.0\nboard = esp32dev\n"
    (tmp_project / "platformio.ini").write_text(ini)
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "7.0.0")
    capsys.readouterr()

    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == ini
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.6.0"
    assert pio_commands() == []
    assert (
        "espressif32: 7.0.0 is outside espressif32@^6.6.0; rerun with --beyond-range"
        in capsys.readouterr().out
    )

    assert pio_lock.update(tmp_project, ["testenv"], beyond_range=True) == 0
    assert "platform = espressif32@^7.0.0\n" in (tmp_project / "platformio.ini").read_text()
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "7.0.0"


def test_update_platform_in_range(tmp_project: Path, platform_update: Any) -> None:
    install, _ = platform_update
    (tmp_project / "platformio.ini").write_text("[env:testenv]\nplatform = espressif32 @ ~6.6.0\n")
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.6.3")
    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert "platform = espressif32 @ ~6.6.3\n" in (tmp_project / "platformio.ini").read_text()


def test_update_edits_platform_in_shared_env_section(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32@6.6.0\n\n[env:a]\nboard = x\n\n[env:b]\nboard = y\n"
    )
    fake = install({"a": _lock(), "b": _lock()})
    assert pio_lock.capture(tmp_project, ["a", "b"]) == 0
    _move_to(fake, "6.7.0")
    capsys.readouterr()

    assert pio_lock.update(tmp_project, ["a", "b"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == (
        "[env]\nplatform = espressif32@6.7.0\n\n[env:a]\nboard = x\n\n[env:b]\nboard = y\n"
    )
    envs = _read_lock(tmp_project)["envs"]
    assert envs["a"]["platform"]["version"] == "6.7.0"
    assert envs["b"]["platform"]["version"] == "6.7.0"
    out = capsys.readouterr().out
    assert out.count("edited platformio.ini: espressif32@6.6.0 => espressif32@6.7.0") == 1
    assert fake.installed_calls == [("a", "platformio/espressif32@6.7.0")]


def test_update_dry_run_platform_writes_nothing(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, pio_commands = platform_update
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    before = (tmp_project / "pio.lock.json").read_text()
    _move_to(fake, "6.7.0")
    capsys.readouterr()
    assert pio_lock.update(tmp_project, ["testenv"], dry_run=True) == 0
    assert (tmp_project / "pio.lock.json").read_text() == before
    assert pio_commands() == []
    assert "would: espressif32: 6.6.0 -> 6.7.0" in capsys.readouterr().out


RELEASE = "https://github.com/pioarduino/platform-espressif32/releases/download/{v}/platform.zip"


def test_update_release_url_platform_edits_ini(
    tmp_project: Path, platform_update: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    install, _ = platform_update
    old, new = RELEASE.format(v="54.03.20"), RELEASE.format(v="54.03.21")
    (tmp_project / "platformio.ini").write_text(f"[env:testenv]\nplatform = {old}\n")
    fake = install({"testenv": _lock(version="54.3.20", src_url=old)})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    fake.by_spec = {new: _lock(version="54.3.21", src_url=new)}

    class GitHub:
        def get_json(self, path: str) -> Any:
            if path == "repos/pioarduino/platform-espressif32/releases?per_page=50":
                return [{"tag_name": "55.03.30"}, {"tag_name": "54.03.21"}]
            return None

    monkeypatch.setattr(pio_lock, "_create_github_client_fn", lambda: GitHub())
    capsys.readouterr()
    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == f"[env:testenv]\nplatform = {new}\n"
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["src_url"] == new
    assert fake.installed_calls == [("testenv", new)]
    out = capsys.readouterr().out
    assert "platform-espressif32: 54.03.20 -> 54.03.21" in out
    assert "55.03.30 is outside" in out


def test_locate_env_option_follows_extends_then_env(tmp_path: Path) -> None:
    main = tmp_path / "platformio.ini"
    main.write_text(
        "[platformio]\nextra_configs = shared.ini\n"
        "[env]\nplatform = espressif32\n"
        "[env:a]\nextends = base\n[env:b]\nboard = y\n[env:c]\nplatform = native\n"
    )
    shared = tmp_path / "shared.ini"
    shared.write_text("[base]\nplatform = espressif32@6.6.0\n")
    files = [main, shared]
    assert pio_lock._locate_env_option(files, "a", "platform") == (
        shared,
        "base",
        "espressif32@6.6.0",
    )
    assert pio_lock._locate_env_option(files, "b", "platform") == (main, "env", "espressif32")
    assert pio_lock._locate_env_option(files, "c", "platform") == (main, "env:c", "native")


def test_locate_env_option_later_extends_win(tmp_path: Path) -> None:
    """PlatformIO walks [env:x], then its extends last-first, then [env]."""
    main = tmp_path / "platformio.ini"
    main.write_text(
        "[env]\nplatform = E\n[a]\nplatform = A\n[b]\nplatform = B\n[c]\nboard = c\n"
        "[env:x]\nextends = a, b\n[env:y]\nextends = a\n[env:z]\nextends = c\n"
    )
    assert pio_lock._locate_env_option([main], "x", "platform") == (main, "b", "B")
    assert pio_lock._locate_env_option([main], "y", "platform") == (main, "a", "A")
    assert pio_lock._locate_env_option([main], "z", "platform") == (main, "env", "E")


def test_requirement_partial_version_is_range() -> None:
    assert pio_lock._requirement_kind("6") == "range"
    assert pio_lock._requirement_kind("6.6") == "range"
    assert pio_lock._requirement_kind("6.6.0") == "exact"
    assert pio_lock._requirement_allows("6", "6.7.0") is True
    assert pio_lock._requirement_allows("6", "7.0.0") is False
    assert pio_lock._requirement_allows("6.6", "6.6.3") is True
    assert pio_lock._build_updated_spec("espressif32@6", "6.7.0") is None


def test_update_partial_platform_version_moves_lock_only(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    ini = "[env:testenv]\nplatform = espressif32@6\n"
    (tmp_project / "platformio.ini").write_text(ini)
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert (tmp_project / "platformio.ini").read_text() == ini
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.7.0"


def test_update_platform_edit_in_shared_section_names_other_envs(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    (tmp_project / "platformio.ini").write_text(
        "[env]\nplatform = espressif32@6.6.0\n[env:a]\n[env:b]\n[env:c]\n"
    )
    fake = install({"a": _lock(), "b": _lock(), "c": _lock()})
    assert pio_lock.capture(tmp_project, ["a", "b", "c"]) == 0
    _move_to(fake, "6.7.0")
    capsys.readouterr()
    assert pio_lock.update(tmp_project, ["a", "b"]) == 0
    envs = _read_lock(tmp_project)["envs"]
    assert envs["a"]["platform"]["version"] == "6.7.0"
    assert envs["c"]["platform"]["version"] == "6.6.0"
    out = capsys.readouterr().out
    assert "[env] also applies to env c; run pio-lock update -e c" in out
    assert "env a;" not in out and "env b;" not in out


def test_update_lib_filter_skips_platforms(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, pio_commands = platform_update
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    capsys.readouterr()
    assert pio_lock.update(tmp_project, ["testenv"], lib_filter="espressif32") == 0
    assert pio_commands() == []
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.6.0"


def test_update_release_url_without_github_notes_it(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, pio_commands = platform_update
    old = RELEASE.format(v="54.03.20")
    (tmp_project / "platformio.ini").write_text(f"[env:testenv]\nplatform = {old}\n")
    install({"testenv": _lock(version="54.3.20", src_url=old)})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    capsys.readouterr()
    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert pio_commands() == []
    assert "GITHUB_TOKEN" in capsys.readouterr().err


# ── Installing a changed platform (capture --install) ────────────────────────


class _RecordingPlatform:
    """A platform object as PlatformFactory.new returns it, recording what is done to it."""

    def __init__(self) -> None:
        self.config: Any = "global ProjectConfig.get_instance()"
        self.project_env: Any = None
        self.configured: list[tuple[str, Any]] = []
        self.required_installed_with: Any = None

    def configure_project_packages(self, env: str, targets: Any = None) -> None:
        self.configured.append((env, self.config))

    def install_required_packages(self, force: bool = False) -> None:
        self.required_installed_with = self.config


def _install_stubs() -> tuple[dict[str, Any], dict[str, Any]]:
    record: dict[str, Any] = {"installs": [], "platform": _RecordingPlatform()}

    class _Manager(_StubPlatformPackageManager):
        def install(self, spec: Any, skip_dependencies: bool = False, **kwargs: Any) -> Any:
            record["installs"].append((spec, skip_dependencies, kwargs))
            return SimpleNamespace(path="/platforms/x")

    factory = SimpleNamespace(new=lambda pkg: record["platform"])
    modules = _stub_modules(
        **{
            "platformio.package.manager.platform": SimpleNamespace(
                PlatformPackageManager=_Manager
            ),
            "platformio.platform.factory": SimpleNamespace(PlatformFactory=factory),
        }
    )
    return modules, record


class _UserView:
    def get(self, section: str, option: str, default: Any = None) -> Any:
        return {"platform": "espressif32@^7", "platform_packages": []}.get(option, default)


def test_real_adapter_install_platform_uses_user_view(tmp_path: Path) -> None:
    """The user's spec is installed without PlatformIO configuring it from the global config
    (which includes pio.lock.ini); its packages are configured from the user's view."""
    modules, record = _install_stubs()
    view = _UserView()

    class _Adapter(_StubAdapter):
        def user_config(self, project_dir: Path) -> Any:
            return view

    _Adapter(modules).install_platform(tmp_path, "esp32")
    assert record["installs"] == [("espressif32@^7", True, {})]
    platform = record["platform"]
    assert platform.configured == [("esp32", view)]
    assert platform.required_installed_with is view
    assert platform.project_env == "esp32"


def test_real_adapter_install_platform_given_spec(tmp_path: Path) -> None:
    modules, record = _install_stubs()

    class _Adapter(_StubAdapter):
        def user_config(self, project_dir: Path) -> Any:
            return _UserView()

    _Adapter(modules).install_platform(tmp_path, "esp32", spec="platformio/espressif32@7.1.0")
    assert record["installs"][0][0] == "platformio/espressif32@7.1.0"


def test_real_adapter_install_platform_failure_is_clean(tmp_path: Path) -> None:
    modules, _record = _install_stubs()
    manager = modules["platformio.package.manager.platform"].PlatformPackageManager

    def install(self: Any, spec: Any, **kwargs: Any) -> Any:
        raise RuntimeError("UnknownPackageError: could not find espressif32@^7")

    manager.install = install

    class _Adapter(_StubAdapter):
        def user_config(self, project_dir: Path) -> Any:
            return _UserView()

    with pytest.raises(pio_lock.PlatformInstallError, match="espressif32@\\^7"):
        _Adapter(modules).install_platform(tmp_path, "esp32")


def test_not_installed_hint_names_capture_install() -> None:
    assert str(PlatformNotInstalled("esp32")) == (
        "platform for env 'esp32' is not installed; run: pio-lock capture --install -e esp32"
    )


def test_capture_install_installs_a_changed_platform(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    fake = fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    (tmp_project / "platformio.ini").write_text(
        "[platformio]\nextra_configs = pio.lock.ini\n[env:testenv]\nplatform = espressif32@^7\n"
    )
    del fake.platforms["testenv"]
    fake.installable = {"espressif32@^7": _lock(version="7.1.0")}

    assert pio_lock.capture(tmp_project, ["testenv"]) == 1
    assert "run: pio-lock capture --install -e testenv" in capsys.readouterr().err
    assert fake.installed_calls == []

    assert pio_lock.capture(tmp_project, ["testenv"], install=True) == 0
    assert fake.installed_calls == [("testenv", None)]
    platform = _read_lock(tmp_project)["envs"]["testenv"]["platform"]
    assert (platform["spec"], platform["version"]) == ("espressif32@^7", "7.1.0")
    assert "platform = platformio/espressif32@7.1.0" in (tmp_project / "pio.lock.ini").read_text()


def test_capture_install_skips_installed_platforms(tmp_project: Path, fake_pio: Any) -> None:
    fake = fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"], install=True) == 0
    assert fake.installed_calls == []


def test_capture_install_failure_is_an_error(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    fake = fake_pio({})
    fake.install_error = pio_lock.PlatformInstallError("could not install espressif32: offline")
    assert pio_lock.capture(tmp_project, ["testenv"], install=True) == 1
    assert "Error: could not install espressif32: offline" in capsys.readouterr().err
    assert not (tmp_project / "pio.lock.json").exists()


def test_cli_capture_install_flag(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(pio_lock, "capture", lambda *a, **k: calls.append(k) or 0)
    assert pio_lock.main(["-d", str(tmp_project), "capture", "-e", "testenv", "--install"]) == 0
    assert calls[-1].get("install") is True
    assert pio_lock.main(["-d", str(tmp_project), "capture", "-e", "testenv"]) == 0
    assert not calls[-1].get("install")


def test_update_installs_platforms_through_the_adapter(
    tmp_project: Path, platform_update: Any
) -> None:
    """``pio pkg install --platform`` would configure the new platform from pio.lock.ini (the
    old pins); the adapter installs it with the user's view."""
    install, pio_commands = platform_update
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    assert pio_lock.update(tmp_project, ["testenv"]) == 0
    assert fake.installed_calls == [("testenv", "platformio/espressif32@6.7.0")]
    assert not any("--platform" in c for c in pio_commands())
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.7.0"


def test_update_platform_install_failure_reports_and_keeps_lock(
    tmp_project: Path, platform_update: Any, capsys: Any
) -> None:
    install, _ = platform_update
    fake = install({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    _move_to(fake, "6.7.0")
    fake.install_error = pio_lock.PlatformInstallError("could not install espressif32: offline")
    assert pio_lock.update(tmp_project, ["testenv"]) == 1
    err = capsys.readouterr().err
    assert "Error: could not install espressif32: offline" in err
    assert "were NOT changed" in err
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.6.0"


# ── Robustness: unreadable lockfile, include missing, probing once ───────────


_CONFLICTED = '{\n<<<<<<< HEAD\n  "lockfile_version": 2,\n=======\n  "lockfile_version": 1,\n'


def test_capture_replaces_an_unreadable_lockfile(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    fake_pio({"testenv": _lock()})
    (tmp_project / "pio.lock.json").write_text(_CONFLICTED)
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    assert _read_lock(tmp_project)["envs"]["testenv"]["platform"]["version"] == "6.6.0"
    err = capsys.readouterr().err
    assert "pio.lock.json is unreadable (" in err
    assert "); previous entries are not kept" in err


@pytest.mark.parametrize("command", ["check", "restore"])
def test_check_and_restore_report_an_unreadable_lockfile(
    tmp_project: Path, fake_pio: Any, capsys: Any, command: str
) -> None:
    fake_pio({"testenv": _lock()})
    (tmp_project / "pio.lock.json").write_text(_CONFLICTED)
    assert getattr(pio_lock, command)(tmp_project, ["testenv"]) == 1
    assert "Error: pio.lock.json is unreadable: " in capsys.readouterr().err


def test_check_without_include_hints_and_skips_platform(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    """Like the build hook: without pio.lock.ini included the platform isn't pinned, so a
    different platform in use is not drift."""
    _locked_project(tmp_project, fake_pio, _lock(version="6.6.0"))
    (tmp_project / "platformio.ini").write_text(
        "[env:testenv]\nplatform = espressif32\nboard = esp32dev\n"
    )
    fake_pio({"testenv": _lock()}, in_use={"testenv": ("espressif32", "6.7.0")})
    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    out = capsys.readouterr().out
    assert "DRIFT" not in out and "6.7.0" not in out
    assert (
        "  platform not compared: pio.lock.ini is not included; add 'extra_configs = "
        "pio.lock.ini' (or run pio-lock init) to pin the platform"
    ) in out


def test_commands_probe_pio_once(
    tmp_project: Path, fake_pio: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[platformio]\nextra_configs = pio.lock.ini\n[env:a]\nplatform = espressif32\n"
        "[env:b]\nplatform = espressif32\n"
    )
    fake = fake_pio({"a": _lock(), "b": _lock()})
    monkeypatch.setattr(pio_lock, "_create_github_client_fn", lambda: None)
    monkeypatch.setattr(pio_lock, "_pio_import_fn", lambda: (None, None, None))
    for command in ("capture", "check", "restore", "outdated", "update"):
        fake.probes = 0
        getattr(pio_lock, command)(tmp_project, ["a", "b"])
        assert fake.probes == 1, command


def test_capture_incompatible_core_is_reported_once(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    (tmp_project / "platformio.ini").write_text(
        "[env:a]\nplatform = espressif32\n[env:b]\nplatform = espressif32\n"
    )
    fake_pio({}, incompatible="platformio.platform.factory.PlatformFactory")
    assert pio_lock.capture(tmp_project, ["a", "b"]) == 0
    err = capsys.readouterr().err
    assert err.count("is not compatible with pio-lock") == 1


def test_real_adapter_lists_installed_tools_once(monkeypatch: Any) -> None:
    calls = {"n": 0}

    class _Tools(_StubToolPackageManager):
        def get_installed(self) -> list[Any]:
            calls["n"] += 1
            return [_tool("tool-a", "1.0.0"), _tool("tool-b", "2.0.0")]

    modules = _stub_modules(
        **{"platformio.package.manager.tool": SimpleNamespace(ToolPackageManager=_Tools)}
    )
    adapter = _StubAdapter(modules)
    assert adapter.package_installed({"name": "tool-a", "version": "1.0.0", "owner": "o"})
    assert adapter.package_installed({"name": "tool-b", "version": "2.0.0", "owner": "o"})
    assert not adapter.package_installed({"name": "tool-b", "version": "2.0.1", "owner": "o"})
    assert calls["n"] == 1


def test_capture_keep_uninstalled_keeps_previous_platform_with_hint(
    tmp_project: Path, fake_pio: Any, capsys: Any
) -> None:
    """What update does for an env whose `platform =` changed and isn't installed."""
    fake = fake_pio({"testenv": _lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0
    (tmp_project / "platformio.ini").write_text("[env:testenv]\nplatform = espressif32@^7\n")
    del fake.platforms["testenv"]
    capsys.readouterr()
    assert pio_lock.capture(tmp_project, ["testenv"], keep_uninstalled=True) == 0
    platform = _read_lock(tmp_project)["envs"]["testenv"]["platform"]
    assert (platform["spec"], platform["version"]) == ("espressif32", "6.6.0")
    err = capsys.readouterr().err
    assert "run: pio-lock capture --install -e testenv" in err
    assert "keeping the previous lock" in err
