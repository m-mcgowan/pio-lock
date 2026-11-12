"""Test doubles for PlatformIO-facing seams (no PlatformIO needed)."""

from __future__ import annotations

import configparser
import dataclasses
import glob
import os
from pathlib import Path
from typing import Any, Optional

from pio_lock import LOCK_INI_NAME, PioIncompatible, PlatformLock, PlatformNotInstalled

_MISSING = object()
_MULTI_VALUE_OPTIONS = frozenset({"platform_packages", "lib_deps", "extra_configs", "extends"})


class FakeConfigView:
    """The slice of PlatformIO's ProjectConfig API pio-lock uses, over plain configparser.

    ``get`` falls back from ``[env:<name>]`` to ``[env]`` and splits multi-value options into
    lists, like ProjectConfig. ``extends`` and interpolation are not modelled.
    """

    def __init__(self, parser: configparser.ConfigParser) -> None:
        self._parser = parser

    def get(self, section: str, option: str, default: Any = _MISSING) -> Any:
        for candidate in (section, "env") if section.startswith("env:") else (section,):
            if self._parser.has_option(candidate, option):
                value = self._parser.get(candidate, option)
                if option in _MULTI_VALUE_OPTIONS:
                    return [v.strip() for v in value.replace(",", "\n").splitlines() if v.strip()]
                return value
        if default is _MISSING:
            raise configparser.NoOptionError(option, section)
        return default

    def envs(self) -> list[str]:
        return [s[4:] for s in self._parser.sections() if s.startswith("env:")]

    def sections(self) -> list[str]:
        return self._parser.sections()


def _read_view(project_dir: Path, include_lock_ini: bool) -> tuple[FakeConfigView, list[Path]]:
    project_dir = Path(project_dir).resolve()
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    main_ini = project_dir / "platformio.ini"
    parser.read(main_ini)
    files = [main_ini]
    lock_ini = os.path.realpath(project_dir / LOCK_INI_NAME)
    view = FakeConfigView(parser)
    for pattern in view.get("platformio", "extra_configs", []):
        full = os.path.join(glob.escape(str(project_dir)), os.path.expanduser(pattern))
        for item in glob.glob(full):
            if not include_lock_ini and os.path.realpath(item) == lock_ini:
                continue
            parser.read(item)
            files.append(Path(item))
    return view, files


class FakePioAdapter:
    """Stands in for ``pio_lock.PioAdapter``; install via ``_pio_adapter_factory``.

    ``platforms`` maps env -> the PlatformLock ``resolve_platform`` returns (missing env ->
    PlatformNotInstalled), with ``spec``/``platform_packages_spec`` read from the user's config
    view as the real adapter does. ``in_use`` maps env -> ``platform_in_use`` result
    (defaults to the locked name/version). ``by_spec`` maps an installed-package spec ->
    PlatformLock for ``resolve_platform(..., installed_spec=...)``. ``installed_packages`` is
    the set of (name, version) ``package_installed`` accepts (default: every package of
    ``platforms``). ``incompatible`` names a call: probe/resolve/in_use then raise PioIncompatible.
    ``installable`` maps a platform spec -> the PlatformLock ``install_platform`` makes
    resolvable (for the env, and by that spec); every call is recorded in ``installed_calls`` as
    (env, spec). ``install_error`` makes ``install_platform`` raise it.
    Config views are real configparser reads of the project.
    """

    def __init__(
        self,
        platforms: dict[str, PlatformLock],
        in_use: Optional[dict[str, tuple[str, str]]] = None,
        incompatible: Optional[str] = None,
        core: str = "6.1.18",
        installed_packages: Optional[set[tuple[str, str]]] = None,
        latest_versions: Optional[dict[str, Optional[str]]] = None,
        by_spec: Optional[dict[str, PlatformLock]] = None,
        installable: Optional[dict[str, PlatformLock]] = None,
    ) -> None:
        self.platforms = platforms
        self.installable = installable or {}
        self.installed_calls: list[tuple[str, Optional[str]]] = []
        self.install_error: Optional[Exception] = None
        self.probes = 0
        self.by_spec = by_spec or {}
        self.resolved_with: dict[str, Optional[str]] = {}
        self.in_use = in_use or {}
        self.incompatible = incompatible
        self.core = core
        self.installed_packages = installed_packages
        self.latest_versions = latest_versions or {}

    def _check(self) -> None:
        if self.incompatible:
            raise PioIncompatible(self.incompatible, self.core, "fake incompatibility")

    def core_version(self) -> str:
        return self.core

    def probe(self) -> None:
        self.probes += 1
        self._check()

    def user_config(self, project_dir: Path) -> FakeConfigView:
        return _read_view(project_dir, include_lock_ini=False)[0]

    def full_config(self, project_dir: Path) -> FakeConfigView:
        return _read_view(project_dir, include_lock_ini=True)[0]

    def config_files(self, project_dir: Path) -> list[Path]:
        return _read_view(project_dir, include_lock_ini=True)[1]

    def resolve_platform(
        self, project_dir: Path, env: str, installed_spec: Optional[str] = None
    ) -> PlatformLock:
        """``installed_spec`` (the installed package to look up) picks from ``by_spec`` when
        listed there; otherwise the env's entry in ``platforms`` is returned."""
        self._check()
        self.resolved_with[env] = installed_spec
        # Like the real adapter, the spec and user platform_packages come from the user's view.
        view = self.user_config(project_dir)
        spec = view.get(f"env:{env}", "platform", None)
        if not spec:
            raise ValueError(f"env '{env}' has no platform")
        if installed_spec is not None and installed_spec in self.by_spec:
            found = self.by_spec[installed_spec]
        elif env in self.platforms:
            found = self.platforms[env]
        else:
            raise PlatformNotInstalled(env)
        return dataclasses.replace(
            found,
            spec=str(spec),
            platform_packages_spec=list(view.get(f"env:{env}", "platform_packages", [])),
        )

    def install_platform(self, project_dir: Path, env: str, spec: Optional[str] = None) -> None:
        self._check()
        self.installed_calls.append((env, spec))
        if self.install_error is not None:
            raise self.install_error
        key = spec or str(self.user_config(project_dir).get(f"env:{env}", "platform", ""))
        if key in self.installable:
            self.by_spec[key] = self.installable[key]
            if spec is None:
                self.platforms[env] = self.installable[key]

    def package_installed(
        self, package: dict[str, Any], project_dir: Optional[Path] = None
    ) -> bool:
        self._check()
        if self.installed_packages is None:
            installed = {
                (p.name, p.version) for lock in self.platforms.values() for p in lock.packages
            }
        else:
            installed = self.installed_packages
        return (package["name"], package["version"]) in installed

    def platform_in_use(self, project_dir: Path, env: str) -> tuple[str, str]:
        self._check()
        if env in self.in_use:
            return self.in_use[env]
        if env not in self.platforms:
            raise PlatformNotInstalled(env)
        lock = self.platforms[env]
        return lock.name, lock.version

    def latest_platform_version(self, name: str, owner: Optional[str] = None) -> Optional[str]:
        self._check()
        return self.latest_versions.get(name)
