"""Tests for the outdated and update commands.

These mock PlatformIO's internal APIs so they run without PIO installed.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

import pio_lock

# ── Mock PIO objects ─────────────────────────────────────────────────────────


class FakeVersion:
    """Minimal stand-in for semantic_version.Version."""

    def __init__(self, ver: str):
        self._ver = ver

    def __str__(self):
        return self._ver

    def __eq__(self, other):
        if isinstance(other, FakeVersion):
            return self._ver == other._ver
        return NotImplemented

    def __ne__(self, other):
        if isinstance(other, FakeVersion):
            return self._ver != other._ver
        return NotImplemented


class FakeOutdatedResult:
    """Stand-in for PackageOutdatedResult."""

    def __init__(
        self,
        current: str,
        latest: str | None = None,
        wanted: str | None = None,
        detached: bool = False,
    ):
        self.current = FakeVersion(current)
        self.latest = FakeVersion(latest) if latest else None
        self.wanted = FakeVersion(wanted) if wanted else None
        self.detached = detached

    def is_outdated(self, allow_incompatible: bool = False) -> bool:
        if self.detached or not self.latest or self.current == self.latest:
            return False
        if allow_incompatible:
            return self.current != self.latest
        if self.wanted:
            return self.current != self.wanted
        return True


class FakePackageMetadata:
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = FakeVersion(version)
        self.spec = MagicMock()
        self.spec.external = False
        self.spec.uri = None


class FakePackageItem:
    def __init__(self, name: str, version: str, external: bool = False):
        self.metadata = FakePackageMetadata(name, version)
        if external:
            self.metadata.spec.external = True
            self.metadata.spec.uri = "git+https://example.com"
        self.path = f"/fake/libdeps/env/{name}"


class FakePackageSpec:
    """Stand-in for PackageSpec."""

    def __init__(self, raw: str):
        self.raw = raw
        name = raw.split("/")[-1].split("@")[0].split("#")[0].strip()
        # Strip .git suffix like real PackageSpec does
        if name.endswith(".git"):
            name = name[:-4]
        self.name = name
        self.external = raw.startswith("http") or raw.startswith("git+")
        self.symlink = False
        self.uri = raw if self.external else None
        if not self.external and self.uri is None and raw.startswith("file://"):
            self.uri = raw
            self.symlink = True


class FakeConfig:
    """Stand-in for ProjectConfig."""

    def __init__(self, lib_deps: dict[str, list[str]], parsed: list[str] | None = None):
        self._lib_deps = lib_deps
        self._parsed = parsed or []

    @classmethod
    def get_instance(cls, _path=None):
        # Will be replaced per-test
        raise NotImplementedError

    def get(self, section: str, option: str, default: Any = None):
        if option == "lib_deps":
            env = section.replace("env:", "")
            return self._lib_deps.get(env, default or [])
        if option == "libdeps_dir":
            return None
        return default


@pytest.fixture
def mock_pio(monkeypatch, tmp_path):
    """Set up mock PIO APIs and return a builder for configuring them."""
    packages: dict[str, FakePackageItem] = {}
    outdated_results: dict[str, FakeOutdatedResult] = {}
    lib_deps: dict[str, list[str]] = {}

    fake_pm = MagicMock()
    fake_pm.get_package = lambda spec: packages.get(spec.name)
    fake_pm.outdated = lambda pkg, spec: outdated_results.get(
        pkg.metadata.name,
        FakeOutdatedResult(str(pkg.metadata.version)),
    )

    def fake_import():
        ini_path = tmp_path / "platformio.ini"
        parsed = [str(ini_path)] if ini_path.exists() else []
        config = FakeConfig(lib_deps, parsed=parsed)

        def fake_config_get_instance(_path=None):
            return config

        pm_cls = MagicMock(return_value=fake_pm)
        return (
            pm_cls,
            FakePackageSpec,
            type(
                "FakeProjectConfig", (), {"get_instance": staticmethod(fake_config_get_instance)}
            ),
        )

    monkeypatch.setattr(pio_lock, "_pio_import_fn", fake_import)
    # Disable GitHub client in unit tests — no real API calls
    monkeypatch.setattr(pio_lock, "_create_github_client_fn", lambda: None)

    class Builder:
        def add_lib(
            self,
            env: str,
            spec_str: str,
            name: str,
            version: str,
            latest: str | None = None,
            wanted: str | None = None,
            external: bool = False,
            detached: bool = False,
        ):
            if env not in lib_deps:
                lib_deps[env] = []
            lib_deps[env].append(spec_str)
            packages[name] = FakePackageItem(name, version, external=external)
            if latest or wanted or detached:
                outdated_results[name] = FakeOutdatedResult(
                    version, latest=latest, wanted=wanted, detached=detached
                )

    return Builder()


# ── Tests for outdated ───────────────────────────────────────────────────────


class TestOutdated:
    def test_all_up_to_date(self, tmp_path, mock_pio):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib("test", "acme/Foo @ ^1.0", "Foo", "1.0.0")
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 0

    def test_registry_outdated(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "1.0.0" in out
        assert "1.2.0" in out
        assert "*" in out

    def test_git_outdated(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib(
            "test",
            "https://github.com/x/y.git#main",
            "y",
            "1.0.0+sha.aaa",
            latest="1.0.0+sha.bbb",
            external=True,
        )
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 1

    def test_detached_skipped(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib(
            "test",
            "acme/Bar @ 2.0",
            "Bar",
            "2.0.0",
            detached=True,
        )
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 0
        assert "pinned" in capsys.readouterr().out

    def test_local_skipped(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        # Local deps are filtered before get_package is called
        mock_pio.add_lib("test", "file://lib/local", "local", "0.0.0")
        # Override to make it look like a local dep
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 0

    def test_json_output(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0",
            "Foo",
            "1.0.0",
            latest="2.0.0",
            wanted="1.5.0",
        )
        rc = pio_lock.outdated(tmp_path, ["test"], output_json=True)
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "libraries" in data
        libs = data["libraries"]
        assert len(libs) == 1
        assert libs[0]["name"] == "Foo"
        assert libs[0]["current"] == "1.0.0"
        assert libs[0]["latest"] == "2.0.0"
        assert libs[0]["wanted"] == "1.5.0"
        assert libs[0]["is_outdated"] is True
        assert "git_deps" in data
        assert "platform" in data

    def test_missing_ini(self, tmp_path):
        rc = pio_lock.outdated(tmp_path, ["test"])
        assert rc == 1

    def test_multiple_envs(self, tmp_path, mock_pio, capsys):
        (tmp_path / "platformio.ini").write_text("[env:a]\n[env:b]\n")
        mock_pio.add_lib("a", "acme/LibA @ 1.0", "LibA", "1.0.0")
        mock_pio.add_lib(
            "b",
            "acme/LibB @ 1.0",
            "LibB",
            "1.0.0",
            latest="2.0.0",
            wanted="2.0.0",
        )
        rc = pio_lock.outdated(tmp_path, ["a", "b"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "LibA" in out
        assert "LibB" in out


# ── Tests for update ─────────────────────────────────────────────────────────


class TestUpdate:
    def test_no_updates_available(self, tmp_path, mock_pio):
        (tmp_path / "platformio.ini").write_text("[env:test]\n")
        mock_pio.add_lib("test", "acme/Foo @ ^1.0", "Foo", "1.0.0")
        rc = pio_lock.update(tmp_path, ["test"])
        assert rc == 0

    def test_dry_run_shows_changes(self, tmp_path, mock_pio, capsys):
        ini_content = "[env:test]\nlib_deps =\n    acme/Foo @ ^1.0.0\n"
        (tmp_path / "platformio.ini").write_text(ini_content)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        rc = pio_lock.update(tmp_path, ["test"], dry_run=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "1.0.0" in out
        assert "1.2.0" in out
        assert "Dry run" in out
        # File should not be modified
        assert (tmp_path / "platformio.ini").read_text() == ini_content

    def test_apply_creates_backup(self, tmp_path, mock_pio):
        ini_content = "[env:test]\nlib_deps =\n    acme/Foo @ ^1.0.0\n"
        (tmp_path / "platformio.ini").write_text(ini_content)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        rc = pio_lock.update(tmp_path, ["test"], dry_run=False)
        assert rc == 0
        assert (tmp_path / "platformio.ini.bak").exists()
        assert (tmp_path / "platformio.ini.bak").read_text() == ini_content

    def test_apply_updates_ini(self, tmp_path, mock_pio):
        ini_content = "[env:test]\nlib_deps =\n    acme/Foo @ ^1.0.0\n"
        (tmp_path / "platformio.ini").write_text(ini_content)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        rc = pio_lock.update(tmp_path, ["test"], dry_run=False)
        assert rc == 0
        new_ini = (tmp_path / "platformio.ini").read_text()
        assert "acme/Foo @ ^1.2.0" in new_ini
        assert "acme/Foo @ ^1.0.0" not in new_ini

    def test_lib_filter(self, tmp_path, mock_pio, capsys):
        ini = "[env:test]\nlib_deps =\n    acme/Foo @ ^1.0\n    acme/Bar @ ^1.0\n"
        (tmp_path / "platformio.ini").write_text(ini)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0",
            "Foo",
            "1.0.0",
            latest="2.0.0",
            wanted="2.0.0",
        )
        mock_pio.add_lib(
            "test",
            "acme/Bar @ ^1.0",
            "Bar",
            "1.0.0",
            latest="3.0.0",
            wanted="3.0.0",
        )
        pio_lock.update(tmp_path, ["test"], lib_filter="Foo")
        out = capsys.readouterr().out
        assert "Foo" in out
        assert "Bar" not in out

    def test_apply_updates_extra_config(self, tmp_path, mock_pio, monkeypatch):
        """Update finds and modifies the correct extra_configs file."""
        # Main ini references extra config but doesn't declare lib_deps
        (tmp_path / "platformio.ini").write_text(
            "[platformio]\nextra_configs = shared.ini\n[env:test]\n"
        )
        # The lib_deps spec lives in the extra config file
        extra_ini = tmp_path / "shared.ini"
        extra_content = "[env:test]\nlib_deps =\n    acme/Foo @ ^1.0.0\n"
        extra_ini.write_text(extra_content)

        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )

        # Patch _pio_import_fn to include both files in _parsed
        original_import = pio_lock._pio_import_fn

        def patched_import():
            pm_cls, spec_cls, config_cls = original_import()
            orig_get_instance = config_cls.get_instance

            def patched_get_instance(_path=None):
                config = orig_get_instance(_path)
                config._parsed = [
                    str(tmp_path / "platformio.ini"),
                    str(extra_ini),
                ]
                return config

            config_cls.get_instance = staticmethod(patched_get_instance)
            return pm_cls, spec_cls, config_cls

        monkeypatch.setattr(pio_lock, "_pio_import_fn", patched_import)

        rc = pio_lock.update(tmp_path, ["test"], dry_run=False)
        assert rc == 0

        # Main ini should be unchanged
        assert "acme/Foo" not in (tmp_path / "platformio.ini").read_text()
        # Extra config should be updated
        new_extra = extra_ini.read_text()
        assert "acme/Foo @ ^1.2.0" in new_extra
        assert "acme/Foo @ ^1.0.0" not in new_extra
        # Backup of extra config
        assert (tmp_path / "shared.ini.bak").exists()
        assert (tmp_path / "shared.ini.bak").read_text() == extra_content

    def test_update_only_rewrites_lib_deps_block(self, tmp_path, mock_pio):
        """Spec strings outside lib_deps (e.g. in comments) must be left alone."""
        ini = (
            "[env:test]\n"
            "; reference: acme/Foo @ ^1.0.0  (do not edit this comment)\n"
            "lib_deps =\n"
            "    acme/Foo @ ^1.0.0\n"
            "build_flags = -DFOO=acme/Foo@^1.0.0\n"
        )
        (tmp_path / "platformio.ini").write_text(ini)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        rc = pio_lock.update(tmp_path, ["test"], dry_run=False)
        assert rc == 0
        new_ini = (tmp_path / "platformio.ini").read_text()
        # Comment must be untouched
        assert "; reference: acme/Foo @ ^1.0.0  (do not edit this comment)" in new_ini
        # build_flags occurrence must be untouched (not in lib_deps block)
        assert "-DFOO=acme/Foo@^1.0.0" in new_ini
        # lib_deps must be updated
        assert "    acme/Foo @ ^1.2.0\n" in new_ini

    def test_update_lib_deps_inline_form(self, tmp_path, mock_pio):
        """`lib_deps = spec` on a single line is rewritten correctly."""
        ini = "[env:test]\nlib_deps = acme/Foo @ ^1.0.0\nbuild_flags = -O2\n"
        (tmp_path / "platformio.ini").write_text(ini)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        pio_lock.update(tmp_path, ["test"], dry_run=False)
        new_ini = (tmp_path / "platformio.ini").read_text()
        assert "lib_deps = acme/Foo @ ^1.2.0\n" in new_ini
        assert "build_flags = -O2\n" in new_ini

    def test_update_lib_deps_with_options_around_it(self, tmp_path, mock_pio):
        """A `lib_deps` block with options before AND after is bounded correctly.

        The regex must stop at the next un-indented line, not consume the
        following options into the block body.
        """
        ini = (
            "[env:test]\n"
            "build_type = release\n"
            "lib_deps =\n"
            "    acme/Foo @ ^1.0.0\n"
            "upload_speed = 921600\n"
            "monitor_speed = 115200\n"
        )
        (tmp_path / "platformio.ini").write_text(ini)
        mock_pio.add_lib(
            "test",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        pio_lock.update(tmp_path, ["test"], dry_run=False)
        new_ini = (tmp_path / "platformio.ini").read_text()
        assert "build_type = release\n" in new_ini
        assert "upload_speed = 921600\n" in new_ini
        assert "monitor_speed = 115200\n" in new_ini
        assert "    acme/Foo @ ^1.2.0\n" in new_ini

    def test_update_same_spec_in_two_envs(self, tmp_path, mock_pio):
        """The same spec declared in two envs' lib_deps must be rewritten in both."""
        ini = (
            "[env:dev]\n"
            "lib_deps =\n"
            "    acme/Foo @ ^1.0.0\n"
            "[env:prod]\n"
            "lib_deps =\n"
            "    acme/Foo @ ^1.0.0\n"
        )
        (tmp_path / "platformio.ini").write_text(ini)
        mock_pio.add_lib(
            "dev",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        # Same spec for prod env
        mock_pio.add_lib(
            "prod",
            "acme/Foo @ ^1.0.0",
            "Foo",
            "1.0.0",
            latest="1.2.0",
            wanted="1.2.0",
        )
        pio_lock.update(tmp_path, ["dev", "prod"], dry_run=False)
        new_ini = (tmp_path / "platformio.ini").read_text()
        assert new_ini.count("acme/Foo @ ^1.2.0") == 2
        assert "acme/Foo @ ^1.0.0" not in new_ini


# ── Tests for ConfigSourceTracker ─────────────────────────────────────────────


class TestConfigSourceTracker:
    def test_tracks_option_source(self, tmp_path):
        """get_source returns the file that defines a (section, option) pair."""
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text("[env:test]\nboard = esp32\n")

        config = MagicMock()
        config._parsed = [str(main_ini)]
        tracker = pio_lock.ConfigSourceTracker(config)

        assert tracker.get_source("env:test", "board") == main_ini

    def test_last_file_wins(self, tmp_path):
        """When multiple files define the same option, last file wins."""
        first = tmp_path / "base.ini"
        first.write_text("[env:test]\nlib_deps = acme/Old @ 1.0\n")
        second = tmp_path / "override.ini"
        second.write_text("[env:test]\nlib_deps = acme/New @ 2.0\n")

        config = MagicMock()
        config._parsed = [str(first), str(second)]
        tracker = pio_lock.ConfigSourceTracker(config)

        assert tracker.get_source("env:test", "lib_deps") == second

    def test_find_file_for_value(self, tmp_path):
        """find_file_for_value locates a spec string in the correct file."""
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text("[env:test]\nlib_deps = acme/Main @ ^1.0\n")
        extra_ini = tmp_path / "shared.ini"
        extra_ini.write_text("[env:test]\nlib_deps = acme/Shared @ ^2.0\n")

        config = MagicMock()
        config._parsed = [str(main_ini), str(extra_ini)]
        tracker = pio_lock.ConfigSourceTracker(config)

        assert tracker.find_file_for_value("acme/Main @ ^1.0") == main_ini
        assert tracker.find_file_for_value("acme/Shared @ ^2.0") == extra_ini
        assert tracker.find_file_for_value("nonexistent") is None

    def test_files_property(self, tmp_path):
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text("[env:test]\n")
        extra = tmp_path / "extra.ini"
        extra.write_text("[env:prod]\n")

        config = MagicMock()
        config._parsed = [str(main_ini), str(extra)]
        tracker = pio_lock.ConfigSourceTracker(config)

        assert tracker.files == [main_ini, extra]

    def test_fallback_path(self, tmp_path):
        """Uses fallback_path when config has no _parsed."""
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text("[env:test]\nboard = esp32\n")

        config = MagicMock(spec=[])  # No _parsed attribute
        del config._parsed
        tracker = pio_lock.ConfigSourceTracker(config, fallback_path=main_ini)

        assert tracker.get_source("env:test", "board") == main_ini


# ── Tests for _build_updated_spec ────────────────────────────────────────────


class TestBuildUpdatedSpec:
    def test_caret_range(self):
        result = pio_lock._build_updated_spec("acme/Foo @ ^1.0.0", "1.2.0")
        assert result == "acme/Foo @ ^1.2.0"

    def test_tilde_range(self):
        result = pio_lock._build_updated_spec("acme/Foo @ ~1.0.0", "1.0.5")
        assert result == "acme/Foo @ ~1.0.5"

    def test_exact_version(self):
        result = pio_lock._build_updated_spec("acme/Foo @ 1.0.4", "1.0.6")
        assert result == "acme/Foo @ 1.0.6"

    def test_gte_range(self):
        result = pio_lock._build_updated_spec("acme/Foo @ >=1.0", "2.0")
        assert result == "acme/Foo @ >=2.0"

    def test_no_at_sign(self):
        result = pio_lock._build_updated_spec("SomeLib", "1.0.0")
        assert result is None

    def test_preserves_owner(self):
        result = pio_lock._build_updated_spec("blues/Blues Wireless Notecard @ ^1.8.3", "1.8.5")
        assert result == "blues/Blues Wireless Notecard @ ^1.8.5"


# ── Tests for GitHub URL parsing ──────────────────────────────────────────────


class TestParseGitHubUrl:
    def test_https_with_ref(self):
        result = pio_lock._parse_github_url("https://github.com/owner/repo.git#v1.0")
        assert result == ("owner", "repo", "v1.0")

    def test_https_without_ref(self):
        result = pio_lock._parse_github_url("https://github.com/owner/repo.git")
        assert result == ("owner", "repo", None)

    def test_https_without_git_suffix(self):
        result = pio_lock._parse_github_url("https://github.com/owner/repo#main")
        assert result == ("owner", "repo", "main")

    def test_sha_ref(self):
        result = pio_lock._parse_github_url(
            "https://github.com/owner/repo#5fddfb83b13057211ca71e000529c3f23609c1d7"
        )
        assert result == ("owner", "repo", "5fddfb83b13057211ca71e000529c3f23609c1d7")

    def test_not_github(self):
        assert pio_lock._parse_github_url("https://gitlab.com/owner/repo") is None

    def test_empty_string(self):
        assert pio_lock._parse_github_url("") is None

    def test_registry_spec(self):
        assert pio_lock._parse_github_url("acme/Foo @ ^1.0") is None


class TestParsePlatformReleaseUrl:
    def test_valid_release_url(self):
        url = "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.36/platform-espressif32.zip"
        result = pio_lock._parse_platform_release_url(url)
        assert result == ("pioarduino", "platform-espressif32", "55.03.36")

    def test_not_a_release_url(self):
        assert pio_lock._parse_platform_release_url("https://github.com/owner/repo") is None

    def test_non_github(self):
        assert pio_lock._parse_platform_release_url("native") is None


# ── Tests for git ref classification ──────────────────────────────────────────


class TestClassifyGitRef:
    def test_sha_40_chars(self):
        assert pio_lock._classify_git_ref("5fddfb83b13057211ca71e000529c3f23609c1d7") == "sha"

    def test_sha_7_chars(self):
        assert pio_lock._classify_git_ref("5fddfb8") == "sha"

    def test_version_tag_with_v(self):
        assert pio_lock._classify_git_ref("v2.0.0") == "tag"

    def test_version_tag_without_v(self):
        assert pio_lock._classify_git_ref("4.2.1") == "tag"

    def test_version_tag_two_parts(self):
        assert pio_lock._classify_git_ref("v2.0") == "tag"

    def test_branch_name(self):
        assert pio_lock._classify_git_ref("fix/notify-characteristics") == "branch"

    def test_branch_main(self):
        assert pio_lock._classify_git_ref("main") == "branch"

    def test_none_is_default(self):
        assert pio_lock._classify_git_ref(None) == "default"


# ── Tests for GitHub-aware dep checks ────────────────────────────────────────


class FakeGitHubClient:
    """Mock GitHubClient that returns pre-configured responses."""

    def __init__(self, responses: dict[str, Any]):
        self._responses = responses

    def get_json(self, api_path: str) -> Optional[Any]:
        return self._responses.get(api_path)


class TestCheckShaDep:
    def test_sha_is_head_of_main(self):
        """SHA that is HEAD of main → up to date, high confidence."""
        sha = "5fddfb83b13057211ca71e000529c3f23609c1d7"
        github = FakeGitHubClient(
            {
                f"repos/owner/repo/commits/{sha}/branches-where-head": [{"name": "main"}],
            }
        )
        result = pio_lock._check_sha_dep("owner", "repo", sha, "my-lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"
        assert result["branch"] == "main"
        assert result["confidence"] == "high"

    def test_sha_is_head_of_feature_branch(self):
        """SHA that is HEAD of a feature branch → up to date, medium confidence."""
        sha = "abc1234567890abcdef1234567890abcdef123456"
        github = FakeGitHubClient(
            {
                f"repos/owner/repo/commits/{sha}/branches-where-head": [
                    {"name": "feature/something"}
                ],
            }
        )
        result = pio_lock._check_sha_dep("owner", "repo", sha, "my-lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"
        assert result["branch"] == "feature/something"
        assert result["confidence"] == "medium"

    def test_sha_behind_default_branch(self):
        """SHA that is behind the default branch → outdated."""
        sha = "oldsha00000000000000000000000000000000000"
        github = FakeGitHubClient(
            {
                f"repos/owner/repo/commits/{sha}/branches-where-head": [],
                f"repos/owner/repo/compare/{sha}...HEAD": {
                    "ahead_by": 5,
                    "commits": [
                        {},
                        {},
                        {},
                        {},
                        {"sha": "newsha99999999999999999999999999999999999"},
                    ],
                },
                "repos/owner/repo": {"default_branch": "main"},
            }
        )
        result = pio_lock._check_sha_dep("owner", "repo", sha, "my-lib", github)
        assert result is not None
        assert result["status"] == "outdated"
        assert result["ahead_by"] == 5
        assert result["branch"] == "main"
        assert result["confidence"] == "high"
        assert "5 commits behind main" in result["message"]

    def test_sha_up_to_date_via_compare(self):
        """SHA that matches HEAD → up to date (via compare fallback)."""
        sha = "currentsha0000000000000000000000000000000"
        github = FakeGitHubClient(
            {
                f"repos/owner/repo/commits/{sha}/branches-where-head": [],
                f"repos/owner/repo/compare/{sha}...HEAD": {"ahead_by": 0},
            }
        )
        result = pio_lock._check_sha_dep("owner", "repo", sha, "my-lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"

    def test_github_api_failure(self):
        """Returns None when GitHub API fails."""
        sha = "abc1234567890abcdef1234567890abcdef123456"
        github = FakeGitHubClient({})  # All requests return None
        result = pio_lock._check_sha_dep("owner", "repo", sha, "my-lib", github)
        # branches-where-head returns None → compare returns None → None
        assert result is None


class TestCheckTagDep:
    def test_latest_tag(self):
        """Pinned to latest tag → up to date."""
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "v2.0.0", "commit": {"sha": "abc"}},
                    {"name": "v1.1.0", "commit": {"sha": "def"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "v2.0.0", "my-lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"
        assert result["message"] == "latest tag"

    def test_newer_tag_available(self):
        """Newer tag exists → outdated."""
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "v3.0.0", "commit": {"sha": "abc"}},
                    {"name": "v2.1.0", "commit": {"sha": "def"}},
                    {"name": "v2.0.0", "commit": {"sha": "ghi"}},
                    {"name": "v1.0.0", "commit": {"sha": "jkl"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "v2.0.0", "my-lib", github)
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest_tag"] == "v3.0.0"
        assert "v3.0.0" in result["message"]

    def test_respects_v_prefix_convention(self):
        """Tags without v prefix shouldn't match v-prefixed pin."""
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "5.0.0", "commit": {"sha": "abc"}},  # no v prefix
                    {"name": "v2.0.0", "commit": {"sha": "def"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "v2.0.0", "my-lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"

    def test_no_v_prefix_finds_newer(self):
        """Tags without v prefix match other non-v tags."""
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "5.0.0", "commit": {"sha": "abc"}},
                    {"name": "4.2.1", "commit": {"sha": "def"}},
                    {"name": "v1.0.0", "commit": {"sha": "ghi"}},  # v prefix, skip
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "4.2.1", "my-lib", github)
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest_tag"] == "5.0.0"

    def test_api_failure(self):
        github = FakeGitHubClient({})
        result = pio_lock._check_tag_dep("owner", "repo", "v1.0", "my-lib", github)
        assert result is None


class TestCheckPlatform:
    def test_newer_release_available(self, tmp_path, monkeypatch):
        """Detects newer platform release."""
        monkeypatch.setattr(
            pio_lock,
            "get_platform_url",
            lambda _d, _e: (
                "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.36/platform-espressif32.zip"
            ),
        )
        github = FakeGitHubClient(
            {
                "repos/pioarduino/platform-espressif32/releases?per_page=50": [
                    {"tag_name": "55.03.37", "draft": False, "prerelease": False},
                    {"tag_name": "55.03.36", "draft": False, "prerelease": False},
                    {"tag_name": "55.03.35", "draft": False, "prerelease": False},
                ],
            }
        )
        result = pio_lock._check_platform(tmp_path, "test", github)
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest"] == "55.03.37"
        assert "55.03.37" in result["message"]

    def test_up_to_date(self, tmp_path, monkeypatch):
        """No newer release → up to date."""
        monkeypatch.setattr(
            pio_lock,
            "get_platform_url",
            lambda _d, _e: (
                "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.37/platform-espressif32.zip"
            ),
        )
        github = FakeGitHubClient(
            {
                "repos/pioarduino/platform-espressif32/releases?per_page=50": [
                    {"tag_name": "55.03.37", "draft": False, "prerelease": False},
                    {"tag_name": "55.03.36", "draft": False, "prerelease": False},
                ],
            }
        )
        result = pio_lock._check_platform(tmp_path, "test", github)
        assert result is not None
        assert result["status"] == "up_to_date"

    def test_skips_prerelease(self, tmp_path, monkeypatch):
        """Prereleases don't count as newer."""
        monkeypatch.setattr(
            pio_lock,
            "get_platform_url",
            lambda _d, _e: (
                "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.36/platform-espressif32.zip"
            ),
        )
        github = FakeGitHubClient(
            {
                "repos/pioarduino/platform-espressif32/releases?per_page=50": [
                    {"tag_name": "55.03.37-rc1", "draft": False, "prerelease": True},
                    {"tag_name": "55.03.36", "draft": False, "prerelease": False},
                ],
            }
        )
        result = pio_lock._check_platform(tmp_path, "test", github)
        assert result is not None
        assert result["status"] == "up_to_date"

    def test_unknown_platform(self, tmp_path, monkeypatch):
        """Non-GitHub platform URL → None."""
        monkeypatch.setattr(pio_lock, "get_platform_url", lambda _d, _e: "native")
        github = FakeGitHubClient({})
        result = pio_lock._check_platform(tmp_path, "test", github)
        assert result is None


class TestCheckGitDeps:
    def test_filters_git_deps_only(self):
        """Only git-type entries are checked."""
        entries = [
            {"name": "Foo", "type": "registry", "spec_str": "acme/Foo @ ^1.0", "current": "1.0"},
            {
                "name": "bar",
                "type": "git",
                "spec_str": "https://github.com/x/bar.git#v1.0",
                "current": "1.0",
            },
        ]
        github = FakeGitHubClient(
            {
                "repos/x/bar/tags?per_page=100": [
                    {"name": "v1.0", "commit": {"sha": "abc"}},
                ],
            }
        )
        results = pio_lock._check_git_deps(entries, github)
        assert len(results) == 1
        assert results[0]["name"] == "bar"

    def test_deduplicates_by_name(self):
        """Same dep across multiple envs is only checked once."""
        entries = [
            {
                "name": "bar",
                "type": "git",
                "spec_str": "https://github.com/x/bar.git#v1.0",
                "current": "1.0",
            },
            {
                "name": "bar",
                "type": "git",
                "spec_str": "https://github.com/x/bar.git#v1.0",
                "current": "1.0",
            },
        ]
        github = FakeGitHubClient(
            {
                "repos/x/bar/tags?per_page=100": [
                    {"name": "v1.0", "commit": {"sha": "abc"}},
                ],
            }
        )
        results = pio_lock._check_git_deps(entries, github)
        assert len(results) == 1


class TestCreateGitHubClient:
    def test_returns_none_without_token_or_gh(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert pio_lock._create_github_client() is None

    def test_prefers_token_over_gh(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        client = pio_lock._create_github_client()
        assert client is not None
        assert client._method == "token"
        assert client._token == "test-token"

    def test_falls_back_to_gh(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/local/bin/gh" if name == "gh" else None
        )
        client = pio_lock._create_github_client()
        assert client is not None
        assert client._method == "gh"


class TestParseVersionTuple:
    def test_semver(self):
        assert pio_lock._parse_version_tuple("1.2.3") == (1, 2, 3)

    def test_with_v_prefix(self):
        assert pio_lock._parse_version_tuple("v2.0.0") == (2, 0, 0)

    def test_two_parts(self):
        assert pio_lock._parse_version_tuple("55.03") == (55, 3)

    def test_three_parts_with_leading_zeros(self):
        assert pio_lock._parse_version_tuple("55.03.36") == (55, 3, 36)

    def test_invalid(self):
        assert pio_lock._parse_version_tuple("not-a-version") is None

    def test_empty(self):
        assert pio_lock._parse_version_tuple("") is None

    def test_prerelease_suffix_dash(self):
        """`54.03.20-rc1` should parse to the numeric prefix, not return None."""
        assert pio_lock._parse_version_tuple("54.03.20-rc1") == (54, 3, 20)

    def test_prerelease_suffix_dot(self):
        assert pio_lock._parse_version_tuple("v2.0.0.beta1") == (2, 0, 0)


# Realistic GitHub tag strings from real-world projects (pioarduino,
# espressif/esp-idf, blues/note-arduino, h2zero/NimBLE-Arduino, …) — used
# to exercise both the version parser and the prerelease classifier
# against the kinds of strings actually published in the wild.
_REAL_TAG_FIXTURES: list[tuple[str, Optional[tuple[int, ...]], bool]] = [
    # (tag, expected version tuple, expected is_prerelease)
    ("55.03.37", (55, 3, 37), False),
    ("v55.03.37", (55, 3, 37), False),
    ("54.03.20-rc1", (54, 3, 20), True),
    ("v2.0.0-rc.1", (2, 0, 0), True),
    ("v3.0.0-beta.2", (3, 0, 0), True),
    ("1.4.1", (1, 4, 1), False),
    ("v5.5.0", (5, 5, 0), False),
    ("v5.5.0-dev-99-g0a1b2c3", (5, 5, 0), True),
    ("0.9.0-alpha", (0, 9, 0), True),
    ("v1.0.0-pre", (1, 0, 0), True),
    ("v2024.11.01", (2024, 11, 1), False),
    # Tag strings that should NOT parse as a version
    ("latest", None, False),
    ("HEAD", None, False),
    ("refs/heads/main", None, False),
    ("release-candidate", None, False),
    ("", None, False),
]


class TestParseVersionTupleRealWorld:
    """Parametrized: parser + prerelease classifier against real GitHub tags."""

    @pytest.mark.parametrize("tag,expected,_is_pre", _REAL_TAG_FIXTURES)
    def test_parses_to_expected_tuple(self, tag, expected, _is_pre):
        assert pio_lock._parse_version_tuple(tag) == expected

    @pytest.mark.parametrize("tag,_expected,is_pre", _REAL_TAG_FIXTURES)
    def test_classifies_prerelease(self, tag, _expected, is_pre):
        assert pio_lock._is_prerelease_tag(tag) == is_pre


class TestCheckTagDepRealWorld:
    """`_check_tag_dep` against realistic mixed-tag GitHub responses."""

    def test_picks_highest_stable_skipping_prereleases(self):
        """When current is stable, latest stable wins even if a higher RC exists."""
        github = FakeGitHubClient(
            {
                "repos/pioarduino/platform-espressif32/tags?per_page=100": [
                    {"name": "55.03.37", "commit": {"sha": "aaa"}},
                    {"name": "55.03.38-rc1", "commit": {"sha": "bbb"}},
                    {"name": "55.03.36", "commit": {"sha": "ccc"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep(
            "pioarduino", "platform-espressif32", "55.03.36", "esp32", github
        )
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest_tag"] == "55.03.37"

    def test_v_prefix_required_to_match_v_prefix(self):
        """A `v`-prefixed pin must not match unprefixed tags and vice versa."""
        github = FakeGitHubClient(
            {
                "repos/h2zero/NimBLE-Arduino/tags?per_page=100": [
                    {"name": "1.4.2", "commit": {"sha": "aaa"}},
                    {"name": "1.4.3", "commit": {"sha": "bbb"}},
                ],
            }
        )
        # Pin without v-prefix → should match unprefixed tags
        result = pio_lock._check_tag_dep(
            "h2zero", "NimBLE-Arduino", "1.4.1", "NimBLE-Arduino", github
        )
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest_tag"] == "1.4.3"

    def test_prerelease_pin_can_advance_to_newer_prerelease(self):
        """If current pin is itself a prerelease, newer prereleases are valid candidates."""
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "v2.0.0-rc1", "commit": {"sha": "aaa"}},
                    {"name": "v2.0.0-rc2", "commit": {"sha": "bbb"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "v2.0.0-rc1", "lib", github)
        assert result is not None
        assert result["status"] == "outdated"
        assert result["latest_tag"] == "v2.0.0-rc2"


class TestCheckPlatformCrossMajor:
    """Cross-major platform releases must surface as a distinct status."""

    def test_higher_major_only_reports_major_update_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            pio_lock,
            "get_platform_url",
            lambda _d, _e: (
                "https://github.com/pioarduino/platform-espressif32/releases/"
                "download/55.03.36/platform-espressif32.zip"
            ),
        )
        github = FakeGitHubClient(
            {
                "repos/pioarduino/platform-espressif32/releases?per_page=50": [
                    {"tag_name": "56.0.0", "draft": False, "prerelease": False},
                    {"tag_name": "55.03.36", "draft": False, "prerelease": False},
                ],
            }
        )
        result = pio_lock._check_platform(tmp_path, "test", github)
        assert result is not None
        assert result["status"] == "major_update_available"
        assert result.get("latest_major") == "56.0.0"


class TestCheckTagDepPrerelease:
    """Stable tag pins must not be advanced to a pre-release tag."""

    def test_stable_not_promoted_to_prerelease(self):
        github = FakeGitHubClient(
            {
                "repos/owner/repo/tags?per_page=100": [
                    {"name": "v2.0.0-rc1", "commit": {"sha": "aaa"}},
                    {"name": "v1.0.0", "commit": {"sha": "bbb"}},
                ],
            }
        )
        result = pio_lock._check_tag_dep("owner", "repo", "v1.0.0", "lib", github)
        assert result is not None
        assert result["status"] == "up_to_date"
