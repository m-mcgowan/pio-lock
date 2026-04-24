"""Shared fixtures for pio-lock tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal PlatformIO project structure."""
    (tmp_path / "platformio.ini").write_text(
        "[env:testenv]\nplatform = espressif32\nboard = esp32dev\n"
    )
    return tmp_path


@pytest.fixture
def make_registry_lib(tmp_path: Path):
    """Factory to create a fake registry library in .pio/libdeps/<env>/."""

    def _make(
        env: str,
        name: str,
        version: str = "1.0.0",
        owner: str = "testowner",
        *,
        project_dir: Path | None = None,
    ) -> Path:
        base = project_dir or tmp_path
        lib_dir = base / ".pio" / "libdeps" / env / name
        lib_dir.mkdir(parents=True, exist_ok=True)
        piopm: dict[str, Any] = {
            "type": "library",
            "name": name,
            "version": version,
            "spec": {"owner": owner, "id": 1234, "name": name, "requirements": None, "uri": None},
        }
        (lib_dir / ".piopm").write_text(json.dumps(piopm))
        return lib_dir

    return _make


@pytest.fixture
def make_git_lib(tmp_path: Path):
    """Factory to create a fake git-sourced library in .pio/libdeps/<env>/."""

    def _make(
        env: str,
        name: str,
        sha: str = "abc123def456",
        url: str = "https://github.com/test/lib.git",
        *,
        project_dir: Path | None = None,
        library_json_name: str | None = None,
    ) -> Path:
        base = project_dir or tmp_path
        lib_dir = base / ".pio" / "libdeps" / env / name
        lib_dir.mkdir(parents=True, exist_ok=True)
        # Create a fake .git marker (file, not real repo)
        (lib_dir / ".git").write_text("fake git marker")
        # Optionally add library.json for display name
        if library_json_name:
            (lib_dir / "library.json").write_text(
                json.dumps({"name": library_json_name, "version": "0.0.0"})
            )
        # Store sha/url for the mock to read
        (lib_dir / ".pio-lock-test-sha").write_text(sha)
        (lib_dir / ".pio-lock-test-url").write_text(url)
        return lib_dir

    return _make


@pytest.fixture
def make_local_lib(tmp_path: Path):
    """Factory to create a fake local (file://) library."""

    def _make(
        env: str,
        name: str,
        uri: str = "file://lib/mylib",
        *,
        project_dir: Path | None = None,
    ) -> Path:
        base = project_dir or tmp_path
        lib_dir = base / ".pio" / "libdeps" / env / name
        lib_dir.mkdir(parents=True, exist_ok=True)
        piopm: dict[str, Any] = {
            "type": "library",
            "name": name,
            "version": "1.0.0",
            "spec": {"owner": None, "id": None, "name": name, "requirements": None, "uri": uri},
        }
        (lib_dir / ".piopm").write_text(json.dumps(piopm))
        return lib_dir

    return _make


@pytest.fixture
def mock_git_commands(monkeypatch: pytest.MonkeyPatch):
    """Replace run_cmd to handle git commands from fake .git markers."""
    import pio_lock

    real_run = pio_lock._default_run_cmd
    call_log: list[list[str]] = []

    def fake_run(args, cwd=None, check=True):
        call_log.append(args)

        if args[:2] == ["git", "rev-parse"] and args[2] == "HEAD":
            # Read from the test marker file
            sha_file = Path(cwd) / ".pio-lock-test-sha"
            if sha_file.exists():
                return sha_file.read_text().strip()
            return "0000000000000000000000000000000000000000"

        if args[:2] == ["git", "rev-parse"] and args[2] == "--short":
            return "abc1234"

        if args[:3] == ["git", "remote", "get-url"]:
            url_file = Path(cwd) / ".pio-lock-test-url"
            if url_file.exists():
                return url_file.read_text().strip()
            return "https://github.com/unknown/unknown.git"

        if args[:2] == ["pio", "system"]:
            return "PlatformIO Core    6.1.18"

        if args[:3] == ["pio", "project", "config"]:
            return json.dumps(
                [
                    ["env:testenv", [["platform", "espressif32"]]],
                ]
            )

        if args[:3] == ["pio", "pkg", "install"]:
            return ""

        # Fall through to real command for anything unexpected
        return real_run(args, cwd=cwd, check=check)

    monkeypatch.setattr(pio_lock, "_run_cmd", fake_run)
    return call_log


@pytest.fixture
def make_global_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create fake global packages and point PLATFORMIO_CORE_DIR at them."""
    packages_dir = tmp_path / "pio_home" / "packages"
    packages_dir.mkdir(parents=True)
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(tmp_path / "pio_home"))

    def _make(name: str, version: str) -> Path:
        pkg_dir = packages_dir / name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "package.json").write_text(json.dumps({"name": name, "version": version}))
        return pkg_dir

    return _make


def write_lockfile(
    project_dir: Path,
    envs: dict[str, list[dict[str, Any]]],
) -> Path:
    """Write a pio.lock.json for testing."""
    lockdata = {
        "_comment": "test lockfile",
        "generated_at": "2026-01-01T00:00:00Z",
        "generated_from_commit": "test123",
        "pio_core_version": "6.1.18",
        "platform_url": "espressif32",
        "global_packages": {},
        "envs": {name: {"libraries": libs} for name, libs in envs.items()},
    }
    path = project_dir / "pio.lock.json"
    path.write_text(json.dumps(lockdata, indent=2))
    return path
