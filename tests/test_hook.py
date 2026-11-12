"""Unit tests for the target-aware build hook."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import pio_lock


class FakeEnv:
    def __init__(self) -> None:
        self.exit_codes: list[int] = []
        self.targets: list[str] = []
        self.vars: dict[str, Any] = {}
        self.options: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.vars.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self.vars[key] = value

    def subst(self, value: str) -> str:
        return value

    def GetProjectOption(self, name: str, default: Any = None) -> Any:  # noqa: N802
        return self.options.get(name, default)

    def Exit(self, code: int) -> None:  # noqa: N802
        self.exit_codes.append(code)

    def AddCustomTarget(self, name: str, *args: Any, **kwargs: Any) -> None:  # noqa: N802
        self.targets.append(name)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PIO_RELEASE_BUILD", "1")
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    # Register for restoration so the hook's SOURCE_DATE_EPOCH does not leak.
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    return tmp_path


def _register(
    env: FakeEnv, project: Path, monkeypatch: pytest.MonkeyPatch, targets: set[str]
) -> None:
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: targets)
    monkeypatch.setattr(env, "subst", lambda _v: str(project))
    pio_lock.register_pio_targets(env)


def test_lock_target_run_skips_release_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = FakeEnv()
    _register(env, project, monkeypatch, {"snapshot-capture"})
    assert env.exit_codes == []


def test_normal_build_still_fails_in_release_mode_without_snapshot(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = FakeEnv()
    _register(env, project, monkeypatch, set())
    assert env.exit_codes == [1]


def test_lock_target_run_still_sets_source_date_epoch(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = FakeEnv()
    _register(env, project, monkeypatch, {"lock-check"})
    assert os.environ.get("SOURCE_DATE_EPOCH")
    assert env.exit_codes == []


def _git_repo(project: Path) -> None:
    import subprocess

    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "one"],
    ):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def test_lock_target_run_does_not_rewrite_stale_snapshot(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "a.txt").write_text("a")
    _git_repo(project)
    snap = project / pio_lock.SNAPSHOT_NAME
    snap.write_text(
        '{"source_date_epoch": 1, "epoch_source": "commit", "build_date": "x",'
        ' "git_commit": "deadbeef", "git_dirty": false, "pinned": false}'
    )
    before = snap.read_bytes()
    env = FakeEnv()
    _register(env, project, monkeypatch, {"snapshot-check"})
    assert snap.read_bytes() == before
    assert env.exit_codes == []


def test_lock_target_run_does_not_create_missing_snapshot(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = FakeEnv()
    _register(env, project, monkeypatch, {"lock-check"})
    assert os.environ.get("SOURCE_DATE_EPOCH")
    assert not (project / pio_lock.SNAPSHOT_NAME).exists()


class _SharedEnv(dict):  # type: ignore[type-arg]
    """Stands in for SCons DefaultEnvironment() (a dict-like construction env)."""


@pytest.fixture
def shared_env(monkeypatch: pytest.MonkeyPatch) -> _SharedEnv:
    import sys
    import types

    shared = _SharedEnv()
    scons = types.ModuleType("SCons")
    script = types.ModuleType("SCons.Script")
    script.DefaultEnvironment = lambda: shared  # type: ignore[attr-defined]
    script.COMMAND_LINE_TARGETS = []  # type: ignore[attr-defined]
    scons.Script = script  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "SCons", scons)
    monkeypatch.setitem(sys.modules, "SCons.Script", script)
    return shared


def test_second_load_registers_nothing_and_warns(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_env: _SharedEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Module is imported as "pio_lock" under pytest; a SConscript exec is not.
    monkeypatch.setattr(pio_lock, "__name__", "__main__")
    first, second = FakeEnv(), FakeEnv()
    _register(first, project, monkeypatch, set())
    capsys.readouterr()
    _register(second, project, monkeypatch, set())
    assert first.targets and second.targets == []
    out = capsys.readouterr().out
    assert (
        "pio-lock: already loaded via lib_deps; the extra_scripts line for pio_lock.py "
        "is deprecated since 0.4.0 and can be removed"
    ) in out


def test_platform_first_load_message_names_platform(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_env: _SharedEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared_env[pio_lock._LOAD_MARKER] = "platform"
    env = FakeEnv()
    _register(env, project, monkeypatch, set())
    assert env.targets == []
    assert (
        "pio-lock: already loaded by the platform (custom_pio_lock); "
        "remove pio-lock from lib_deps or extra_scripts"
    ) in capsys.readouterr().out


# ── Build-time drift checks ──────────────────────────────────────────────────

_INCLUDE = "[platformio]\nextra_configs = pio.lock.ini\n"
_ENV = "[env:testenv]\nplatform = espressif32\nboard = esp32dev\n"


def _platform_lock(version: str = "6.6.0") -> pio_lock.PlatformLock:
    return pio_lock.PlatformLock(
        spec="espressif32",
        platform_packages_spec=[],
        name="espressif32",
        version=version,
        src_url=None,
        packages=[],
    )


@pytest.fixture
def build(
    tmp_project: Path, fake_pio: Any, make_registry_lib: Any, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """A captured project (MyLib 1.0.0, espressif32 6.6.0) and a runner for build_checks."""
    monkeypatch.delenv("PIO_RELEASE_BUILD", raising=False)
    monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    monkeypatch.delenv("PIO_LOCK_ALLOW_OVERRIDE", raising=False)
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: set())
    monkeypatch.setattr(pio_lock, "_is_interactive", lambda: False)
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    (tmp_project / "platformio.ini").write_text(_INCLUDE + _ENV)
    fake_pio({"testenv": _platform_lock()})
    assert pio_lock.capture(tmp_project, ["testenv"]) == 0

    class Build:
        project = tmp_project
        env = FakeEnv()

        def release(self) -> None:
            monkeypatch.setenv("PIO_RELEASE_BUILD", "1")

        def drift_library(self) -> None:
            make_registry_lib("testenv", "MyLib", "1.1.0", project_dir=tmp_project)

        def run(self) -> list[int]:
            pio_lock.build_checks(tmp_project, "testenv", self.env)
            return self.env.exit_codes

    return Build()


def test_no_lockfile_no_checks(
    tmp_project: Path, fake_pio: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("PIO_RELEASE_BUILD", "1")
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: set())
    fake_pio({}, incompatible="platformio.x")
    env = FakeEnv()
    pio_lock.build_checks(tmp_project, "testenv", env)
    assert env.exit_codes == []
    assert capsys.readouterr().out == ""


def test_matching_build_is_quiet(build: Any, capsys: Any) -> None:
    build.release()
    assert build.run() == []
    assert capsys.readouterr().out == ""


def test_library_drift_warns_outside_release(build: Any, capsys: Any) -> None:
    build.drift_library()
    assert build.run() == []
    out = capsys.readouterr().out
    assert "WARNING: MyLib — expected 1.0.0" in out
    assert "run: pio-lock restore -e testenv" in out


def test_library_drift_fails_in_release_mode(build: Any, capsys: Any) -> None:
    build.release()
    build.drift_library()
    assert build.run() == [1]
    captured = capsys.readouterr()
    assert "ERROR: MyLib — expected 1.0.0" in captured.out
    assert "run: pio-lock restore -e testenv" in captured.out
    assert "PIO_LOCK_DRIFT=warn" in captured.err


@pytest.mark.parametrize("via", ["env_var", "ini_option"])
def test_drift_policy_warn_downgrades_release_failure(
    build: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    build.release()
    build.drift_library()
    if via == "env_var":
        monkeypatch.setenv("PIO_LOCK_DRIFT", "warn")
    else:
        build.env.options["custom_pio_lock_drift"] = "warn"
    assert build.run() == []
    assert "WARNING: MyLib — expected 1.0.0" in capsys.readouterr().out


def test_drift_policy_env_var_wins_over_ini(monkeypatch: pytest.MonkeyPatch) -> None:
    env = FakeEnv()
    monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    assert pio_lock.drift_policy(env) == "fail"
    env.options["custom_pio_lock_drift"] = " Warn "
    assert pio_lock.drift_policy(env) == "warn"
    monkeypatch.setenv("PIO_LOCK_DRIFT", "fail")
    assert pio_lock.drift_policy(env) == "fail"


def test_v1_lockfile_platform_hint_only(
    tmp_project: Path,
    fake_pio: Any,
    make_registry_lib: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    from tests.conftest import write_lockfile

    monkeypatch.setenv("PIO_RELEASE_BUILD", "1")
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: set())
    fake_pio({"testenv": _platform_lock()}, in_use={"testenv": ("espressif32", "6.7.0")})
    make_registry_lib("testenv", "MyLib", "1.0.0", project_dir=tmp_project)
    write_lockfile(
        tmp_project,
        {"testenv": [{"name": "MyLib", "type": "registry", "version": "1.0.0", "owner": "o"}]},
    )
    env = FakeEnv()
    pio_lock.build_checks(tmp_project, "testenv", env)
    assert env.exit_codes == []
    out = capsys.readouterr().out
    assert "platform not checked: lockfile is v1; run: pio-lock capture -e testenv" in out
    assert "6.7.0" not in out


def test_include_missing_is_hint_not_failure(build: Any, fake_pio: Any, capsys: Any) -> None:
    (build.project / "platformio.ini").write_text(_ENV)
    fake_pio({"testenv": _platform_lock()}, in_use={"testenv": ("espressif32", "6.7.0")})
    build.release()
    assert build.run() == []
    out = capsys.readouterr().out
    assert (
        "pio-lock: add 'extra_configs = pio.lock.ini' (or run pio-lock init) to pin the platform"
    ) in out
    assert "6.7.0" not in out


def test_platform_drift_fails_in_release_mode(build: Any, fake_pio: Any, capsys: Any) -> None:
    (build.project / "platformio.ini").write_text(
        _INCLUDE + "[env:testenv]\nplatform = espressif32@^7\n"
    )
    build.release()
    assert build.run() == [1]
    out = capsys.readouterr().out
    assert "ERROR: platform spec changed: 'espressif32' → 'espressif32@^7'" in out
    assert "run: pio-lock capture -e testenv" in out


# Same spec as platformio.ini, so the lock is not stale: only the override check can catch it.
_OVERRIDE = "[env:testenv]\nplatform = espressif32\n"


def _with_override(project: Path, *, glob_include: bool = False) -> None:
    """Add configs/zz.ini, read after pio.lock.ini, overriding the locked platform."""
    (project / "configs").mkdir()
    (project / "configs" / "zz.ini").write_text(_OVERRIDE)
    (project / "configs" / "aa.ini").write_text("[env:other]\nplatform = native\n")
    extra = "pio.lock.ini\n  configs/*.ini" if glob_include else "pio.lock.ini, configs/zz.ini"
    (project / "platformio.ini").write_text(f"[platformio]\nextra_configs = {extra}\n" + _ENV)


@pytest.mark.parametrize("glob_include", [False, True])
def test_override_after_lock_ini_is_an_error(build: Any, capsys: Any, glob_include: bool) -> None:
    _with_override(build.project, glob_include=glob_include)
    build.release()
    assert build.run() == [1]
    out = capsys.readouterr().out
    assert "ERROR: configs/zz.ini sets platform for env 'testenv' after pio.lock.ini" in out
    assert "aa.ini" not in out
    assert "PIO_LOCK_ALLOW_OVERRIDE=1" in out


def test_override_before_lock_ini_is_not_an_error(build: Any, capsys: Any) -> None:
    (build.project / "early.ini").write_text(_OVERRIDE)
    (build.project / "platformio.ini").write_text(
        "[platformio]\nextra_configs = early.ini\n  pio.lock.ini\n" + _ENV
    )
    build.release()
    assert build.run() == []
    assert "early.ini" not in capsys.readouterr().out


@pytest.mark.parametrize("via", ["env_var", "ini_option"])
def test_override_allowed_downgrades_to_warning(
    build: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    _with_override(build.project)
    build.release()
    if via == "env_var":
        monkeypatch.setenv("PIO_LOCK_ALLOW_OVERRIDE", "1")
    else:
        build.env.options["custom_pio_lock_allow_override"] = "true"
    assert build.run() == []
    out = capsys.readouterr().out
    assert "WARNING: configs/zz.ini sets platform for env 'testenv' after pio.lock.ini" in out


def test_core_version_mismatch_warns_only(build: Any, fake_pio: Any, capsys: Any) -> None:
    fake_pio({"testenv": _platform_lock()}, core="6.2.0")
    build.release()
    assert build.run() == []
    out = capsys.readouterr().out
    assert "WARNING: PlatformIO Core 6.2.0 differs from the locked 6.1.18" in out


def test_incompatible_pio_warns_outside_release(build: Any, fake_pio: Any, capsys: Any) -> None:
    fake_pio({"testenv": _platform_lock()}, incompatible="platformio.x.y")
    assert build.run() == []
    out = capsys.readouterr().out
    assert "WARNING: platform not checked: PlatformIO Core 6.1.18 is not compatible" in out


def test_incompatible_pio_fails_release_with_v2_lock(
    build: Any, fake_pio: Any, capsys: Any
) -> None:
    fake_pio({"testenv": _platform_lock()}, incompatible="platformio.x.y")
    build.release()
    assert build.run() == [1]
    assert "ERROR: platform not checked: PlatformIO Core" in capsys.readouterr().out


def test_interactive_abort_exits(build: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    build.drift_library()
    monkeypatch.setattr(pio_lock, "_is_interactive", lambda: True)
    prompts: list[str] = []
    answers = iter(["x", "a"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    assert build.run() == [1]
    assert prompts == ["Continue with this build? [c]ontinue / [a]bort: "] * 2
    assert "WARNING: MyLib — expected 1.0.0" in capsys.readouterr().out


def test_interactive_continue_proceeds(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    build.drift_library()
    monkeypatch.setattr(pio_lock, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "c")
    assert build.run() == []


def test_interactive_release_never_prompts(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    build.drift_library()
    build.release()
    monkeypatch.setattr(pio_lock, "_is_interactive", lambda: True)

    def no_input(_prompt: str) -> str:
        raise AssertionError("prompted in release mode")

    monkeypatch.setattr("builtins.input", no_input)
    assert build.run() == [1]


def test_lock_target_run_skips_checks(
    build: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    build.drift_library()
    build.release()
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: {"lock-restore"})
    assert build.run() == []
    assert capsys.readouterr().out == ""


def test_register_runs_build_checks_for_current_env(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(pio_lock, "build_checks", lambda p, name, _env: calls.append((p, name)))
    monkeypatch.setattr(pio_lock, "snapshot_auto_manage", lambda *a, **k: None)
    monkeypatch.setattr(pio_lock, "_requested_targets", lambda _env: set())
    env = FakeEnv()
    monkeypatch.setattr(
        env, "subst", lambda v: {"$PROJECT_DIR": str(project), "$PIOENV": "native"}.get(v, v)
    )
    pio_lock.register_pio_targets(env)
    assert calls == [(project, "native")]
