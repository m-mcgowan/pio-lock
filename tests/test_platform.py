"""Unit tests for PioAdapter, its fake, and running the CLI under PlatformIO's Python.

No PlatformIO needed: the real adapter is driven through stub modules returned by an
overridden ``_import``; everything else uses ``tests.fakes.FakePioAdapter``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import pio_lock
from pio_lock import PioAdapter, PioIncompatible, PlatformLock, PlatformNotInstalled
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
    assert fake.resolve_platform(tmp_project, "testenv") is lock
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
