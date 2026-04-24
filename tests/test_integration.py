"""Integration tests — require PlatformIO CLI installed.

These tests run against real (but minimal) PlatformIO projects.
They do NOT download full platforms/toolchains — only library operations.

Run with:  pytest -m integration
Skip with: pytest -m "not integration"

To add a test case from a bug report:
  1. Create a directory under tests/fixtures/<issue-name>/
  2. Add a platformio.ini with the minimal config that reproduces the issue
  3. Add a README.md describing the expected behavior
  4. Add pre-installed library state if needed (fake .piopm / .git markers)
  5. Write a test function below that uses the fixture
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import pio_lock

pytestmark = pytest.mark.integration


def have_pio() -> bool:
    """Check if PlatformIO CLI is available."""
    try:
        subprocess.run(
            ["pio", "--version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_pio = pytest.mark.skipif(not have_pio(), reason="PlatformIO CLI not available")


def _get_pio_site_packages() -> str | None:
    """Find PIO's Python site-packages directory for import access."""
    try:
        result = subprocess.run(
            ["pio", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        # Read the shebang from `pio` to find its Python
        pio_path = subprocess.run(
            ["which", "pio"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not pio_path:
            return None
        with open(pio_path) as f:
            shebang = f.readline().strip()
        if not shebang.startswith("#!"):
            return None
        pio_python = shebang[2:]
        result = subprocess.run(
            [pio_python, "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


@pytest.fixture
def pio_imports():
    """Make PlatformIO importable by adding its site-packages to sys.path."""
    site_pkgs = _get_pio_site_packages()
    if not site_pkgs:
        pytest.skip("Cannot find PlatformIO's Python site-packages")
    if site_pkgs not in sys.path:
        sys.path.insert(0, site_pkgs)
    from platformio.project.config import ProjectConfig

    return ProjectConfig


@skip_no_pio
class TestCaptureIntegration:
    """Integration tests for capture against a real PIO project."""

    def test_capture_minimal_project(self, tmp_path: Path):
        """Capture works on a bare project with no installed deps."""
        (tmp_path / "platformio.ini").write_text("[env:native]\nplatform = native\n")
        # Create empty libdeps dir (as if pio pkg install ran with no libs)
        (tmp_path / ".pio" / "libdeps" / "native").mkdir(parents=True)

        result = subprocess.run(
            [
                "python",
                str(Path(__file__).parent.parent / "pio_lock.py"),
                "-d",
                str(tmp_path),
                "capture",
                "-e",
                "native",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr

        lockfile = tmp_path / "pio.lock.json"
        assert lockfile.exists()
        data = json.loads(lockfile.read_text())
        assert "native" in data["envs"]
        assert data["pio_core_version"] != "unknown"

    def test_capture_then_check_roundtrip(self, tmp_path: Path):
        """Capture followed by check should always pass."""
        (tmp_path / "platformio.ini").write_text("[env:native]\nplatform = native\n")
        (tmp_path / ".pio" / "libdeps" / "native").mkdir(parents=True)

        script = str(Path(__file__).parent.parent / "pio_lock.py")

        # Capture
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        # Check
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "check", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "passed" in result.stdout


def _make_git_lib(parent: Path, name: str, version: str = "1.0.0") -> str:
    """Create a minimal git repo that looks like a PIO git library. Returns the SHA."""
    lib_dir = parent / name
    lib_dir.mkdir()
    lib_json = {"name": name, "version": version}
    (lib_dir / "library.json").write_text(json.dumps(lib_json))
    (lib_dir / f"{name}.h").write_text(f"// {name}\n")
    subprocess.run(["git", "init"], cwd=str(lib_dir), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(lib_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(lib_dir),
        capture_output=True,
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(lib_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_piopm(name: str, version: str, owner: str = "") -> str:
    """Build a .piopm JSON string for a fake registry library."""
    meta = {
        "type": "library",
        "name": name,
        "version": version,
        "spec": {"owner": owner, "id": None, "name": name, "uri": None},
    }
    return json.dumps(meta)


@skip_no_pio
class TestGitLibCapture:
    """Tests for capturing git-based library dependencies."""

    def _setup_project(self, tmp_path, lib_deps_line):
        ini = f"[env:native]\nplatform = native\nlib_deps =\n    {lib_deps_line}\n"
        (tmp_path / "platformio.ini").write_text(ini)
        libdeps = tmp_path / ".pio" / "libdeps" / "native"
        libdeps.mkdir(parents=True)
        return libdeps

    def test_capture_git_lib_tag_ref(self, tmp_path):
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#v1.0")
        sha = _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        lib = data["envs"]["native"]["libraries"][0]
        assert lib["type"] == "git"
        assert lib["sha"] == sha
        assert lib["name"] == "y"

    def test_capture_git_lib_sha_ref(self, tmp_path):
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#abc123")
        sha = _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        assert data["envs"]["native"]["libraries"][0]["sha"] == sha

    def test_capture_git_lib_branch_ref(self, tmp_path):
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#main")
        sha = _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        assert data["envs"]["native"]["libraries"][0]["sha"] == sha

    def test_capture_git_lib_unpinned(self, tmp_path):
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git")
        sha = _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        assert data["envs"]["native"]["libraries"][0]["sha"] == sha

    def test_capture_git_lib_uses_library_json_name(self, tmp_path):
        """Git lib name comes from library.json, not the directory name."""
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#v1.0")
        _make_git_lib(libdeps, "y")
        # Override library.json with a different name
        lib_json = {"name": "YLibrary", "version": "1.0.0"}
        (libdeps / "y" / "library.json").write_text(json.dumps(lib_json))

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        assert data["envs"]["native"]["libraries"][0]["name"] == "YLibrary"

    def test_capture_then_check_git_roundtrip(self, tmp_path):
        """Capture then check passes for git libs."""
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#main")
        _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "check", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "passed" in result.stdout

    def test_check_detects_git_drift(self, tmp_path):
        """Check detects when a git lib SHA has changed."""
        libdeps = self._setup_project(tmp_path, "https://github.com/x/y.git#main")
        _make_git_lib(libdeps, "y")

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        # Capture
        subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
            check=True,
        )

        # Make a new commit to simulate drift
        lib_dir = libdeps / "y"
        (lib_dir / "new_file.h").write_text("// new\n")
        subprocess.run(["git", "add", "."], cwd=str(lib_dir), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "drift"],
            cwd=str(lib_dir),
            capture_output=True,
            check=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        # Check should now fail
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "check", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "DRIFT" in result.stdout


@skip_no_pio
class TestShadowCopyHandling:
    """Tests that PIO shadow copy directories are skipped."""

    def test_shadow_copy_not_captured(self, tmp_path):
        ini = "[env:native]\nplatform = native\n"
        (tmp_path / "platformio.ini").write_text(ini)
        libdeps = tmp_path / ".pio" / "libdeps" / "native"
        libdeps.mkdir(parents=True)

        # Real library
        real_dir = libdeps / "NimBLE-Arduino"
        real_dir.mkdir()
        (real_dir / ".piopm").write_text(_make_piopm("NimBLE-Arduino", "1.4.1", "h2zero"))

        # Shadow copy (should be skipped)
        shadow_dir = libdeps / "NimBLE-Arduino@src-abc123"
        shadow_dir.mkdir()
        (shadow_dir / ".piopm").write_text(_make_piopm("NimBLE-Arduino", "1.4.1", "h2zero"))

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "native"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        libs = data["envs"]["native"]["libraries"]
        names = [lib["name"] for lib in libs]
        assert names.count("NimBLE-Arduino") == 1


@skip_no_pio
class TestMultipleEnvs:
    """Tests for multi-environment capture."""

    def test_capture_multiple_envs(self, tmp_path):
        ini = "[env:a]\nplatform = native\n[env:b]\nplatform = native\n"
        (tmp_path / "platformio.ini").write_text(ini)
        for env in ("a", "b"):
            env_dir = tmp_path / ".pio" / "libdeps" / env
            env_dir.mkdir(parents=True)
            lib_dir = env_dir / f"Lib{env.upper()}"
            lib_dir.mkdir()
            (lib_dir / ".piopm").write_text(_make_piopm(f"Lib{env.upper()}", "1.0.0"))

        script = str(Path(__file__).parent.parent / "pio_lock.py")
        result = subprocess.run(
            ["python", script, "-d", str(tmp_path), "capture", "-e", "a", "-e", "b"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads((tmp_path / "pio.lock.json").read_text())
        assert "a" in data["envs"]
        assert "b" in data["envs"]
        assert data["envs"]["a"]["libraries"][0]["name"] == "LibA"
        assert data["envs"]["b"]["libraries"][0]["name"] == "LibB"


@skip_no_pio
class TestFixtureProjects:
    """Tests that load fixture projects from tests/fixtures/.

    To reproduce a bug report, create a fixture directory with:
      - platformio.ini
      - .pio/libdeps/<env>/  (with fake library state)
      - expected.json (optional — expected lockfile output)
      - README.md (describe the scenario)
    """

    FIXTURES_DIR = Path(__file__).parent / "fixtures"

    def _fixture_dirs(self):
        if not self.FIXTURES_DIR.is_dir():
            return []
        return [
            d
            for d in sorted(self.FIXTURES_DIR.iterdir())
            if d.is_dir() and (d / "platformio.ini").exists()
        ]

    def test_fixture_projects_capture(self):
        """Each fixture project should capture successfully."""
        fixtures = self._fixture_dirs()
        if not fixtures:
            pytest.skip("No fixture projects found")

        script = str(Path(__file__).parent.parent / "pio_lock.py")

        for fixture_dir in fixtures:
            ini = (fixture_dir / "platformio.ini").read_text()
            # Extract env names from [env:xxx] sections
            envs = []
            for line in ini.splitlines():
                line = line.strip()
                if line.startswith("[env:") and line.endswith("]"):
                    envs.append(line[5:-1])
            if not envs:
                continue

            env_args = []
            for env in envs:
                env_args.extend(["-e", env])

            result = subprocess.run(
                ["python", script, "-d", str(fixture_dir), "capture", *env_args],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"Fixture {fixture_dir.name} failed:\n{result.stderr}"

            # If expected.json exists, compare library lists
            expected_path = fixture_dir / "expected.json"
            if expected_path.exists():
                actual = json.loads((fixture_dir / "pio.lock.json").read_text())
                expected = json.loads(expected_path.read_text())
                for env in envs:
                    actual_libs = {lib["name"]: lib for lib in actual["envs"][env]["libraries"]}
                    for exp_lib in expected.get(env, []):
                        name = exp_lib["name"]
                        assert name in actual_libs, f"Fixture {fixture_dir.name}: missing {name}"
                        for key, val in exp_lib.items():
                            assert actual_libs[name].get(key) == val, (
                                f"Fixture {fixture_dir.name}: {name}.{key} "
                                f"expected {val}, got {actual_libs[name].get(key)}"
                            )


@skip_no_pio
class TestConfigSourceTrackerIntegration:
    """Test ConfigSourceTracker against real PIO ProjectConfig."""

    def test_single_file_tracking(self, tmp_path: Path, pio_imports, monkeypatch):
        """ConfigSourceTracker tracks options from a single platformio.ini."""
        config_cls = pio_imports
        monkeypatch.chdir(tmp_path)
        ini = tmp_path / "platformio.ini"
        ini.write_text("[env:native]\nplatform = native\nlib_deps =\n    acme/Foo @ ^1.0\n")

        config = config_cls(str(ini))
        tracker = pio_lock.ConfigSourceTracker(config)

        assert len(tracker.files) == 1
        assert tracker.get_source("env:native", "platform") is not None
        assert tracker.get_source("env:native", "lib_deps") is not None
        assert tracker.find_file_for_value("acme/Foo @ ^1.0") is not None

    def test_extra_configs_tracking(self, tmp_path: Path, pio_imports, monkeypatch):
        """ConfigSourceTracker identifies which extra_configs file defines each option."""
        config_cls = pio_imports
        monkeypatch.chdir(tmp_path)
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text(
            "[platformio]\nextra_configs = shared/libs.ini\n\n[env:native]\nplatform = native\n"
        )

        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        libs_ini = shared_dir / "libs.ini"
        libs_ini.write_text("[env:native]\nlib_deps =\n    acme/Foo @ ^1.0\n    acme/Bar @ ^2.0\n")

        config = config_cls(str(main_ini))
        tracker = pio_lock.ConfigSourceTracker(config)

        # Should see both files
        assert len(tracker.files) == 2

        # find_file_for_value locates specs in the extra config file
        found = tracker.find_file_for_value("acme/Foo @ ^1.0")
        assert found is not None
        assert found.name == "libs.ini"
        found2 = tracker.find_file_for_value("acme/Bar @ ^2.0")
        assert found2 is not None
        assert found2.name == "libs.ini"

    def test_extra_configs_glob_pattern(self, tmp_path: Path, pio_imports, monkeypatch):
        """ConfigSourceTracker works with glob patterns in extra_configs."""
        config_cls = pio_imports
        monkeypatch.chdir(tmp_path)
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text(
            "[platformio]\nextra_configs = conf/*.ini\n\n[env:native]\nplatform = native\n"
        )

        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()

        libs_ini = conf_dir / "libs.ini"
        libs_ini.write_text("[env:native]\nlib_deps =\n    acme/Foo @ ^1.0\n")

        test_ini = conf_dir / "test.ini"
        test_ini.write_text("[env:native]\ntest_framework = doctest\n")

        config = config_cls(str(main_ini))
        tracker = pio_lock.ConfigSourceTracker(config)

        # All three files should be tracked (main + 2 globs)
        assert len(tracker.files) >= 3

        # find_file_for_value locates lib spec in libs.ini
        found = tracker.find_file_for_value("acme/Foo @ ^1.0")
        assert found is not None
        assert found.name == "libs.ini"

        # test_framework comes from test.ini
        source = tracker.get_source("env:native", "test_framework")
        assert source is not None
        assert source.name == "test.ini"

    def test_multiple_extra_configs_with_overlapping_lib_deps(
        self, tmp_path: Path, pio_imports, monkeypatch
    ):
        """When lib_deps appears in multiple files, find_file_for_value finds each spec."""
        config_cls = pio_imports
        monkeypatch.chdir(tmp_path)
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text(
            "[platformio]\n"
            "extra_configs =\n"
            "    shared/base.ini\n"
            "    shared/extras.ini\n"
            "\n"
            "[env:native]\n"
            "platform = native\n"
        )

        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()

        base_ini = shared_dir / "base.ini"
        base_ini.write_text("[env:native]\nlib_deps =\n    acme/CoreLib @ ^1.0\n")

        extras_ini = shared_dir / "extras.ini"
        extras_ini.write_text("[env:native]\nlib_deps =\n    acme/ExtraLib @ ^2.0\n")

        config = config_cls(str(main_ini))
        tracker = pio_lock.ConfigSourceTracker(config)

        # Each spec found in its respective file
        core_file = tracker.find_file_for_value("acme/CoreLib @ ^1.0")
        assert core_file is not None
        assert core_file.name == "base.ini"
        extra_file = tracker.find_file_for_value("acme/ExtraLib @ ^2.0")
        assert extra_file is not None
        assert extra_file.name == "extras.ini"

    def test_parsed_attribute_populated(self, tmp_path: Path, pio_imports, monkeypatch):
        """Verify PIO's ProjectConfig._parsed contains expected files."""
        config_cls = pio_imports
        monkeypatch.chdir(tmp_path)
        main_ini = tmp_path / "platformio.ini"
        main_ini.write_text(
            "[platformio]\nextra_configs = extra.ini\n\n[env:native]\nplatform = native\n"
        )

        extra_ini = tmp_path / "extra.ini"
        extra_ini.write_text("[env:native]\nboard = esp32dev\n")

        config = config_cls(str(main_ini))

        # Verify the _parsed attribute exists and contains both files
        assert hasattr(config, "_parsed")
        assert len(config._parsed) == 2
        # First entry is the main ini (absolute path), second is the extra (relative)
        filenames = [Path(p).name for p in config._parsed]
        assert "platformio.ini" in filenames
        assert "extra.ini" in filenames
