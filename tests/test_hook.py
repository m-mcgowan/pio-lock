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

    def subst(self, value: str) -> str:
        return value

    def GetProjectOption(self, name: str, default: Any = None) -> Any:  # noqa: N802
        return default

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
