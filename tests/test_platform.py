"""Unit tests for PioAdapter, its fake, and running the CLI under PlatformIO's Python.

No PlatformIO needed: the real adapter is driven through stub modules returned by an
overridden ``_import``; everything else uses ``tests.fakes.FakePioAdapter``.
"""

from __future__ import annotations

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
            ProjectConfig=SimpleNamespace(read=None, get=None, items=None)
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
