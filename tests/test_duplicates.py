"""Duplicate installed copies of one library: detection, reporting, and the commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pio_lock
from tests.conftest import espressif32_lock, write_lockfile

UBLOX = "SparkFun u-blox GNSS v3"


def _ini(project: Path, lib_deps: str = "") -> None:
    body = f"lib_deps =\n    {lib_deps}\n" if lib_deps else ""
    (project / "platformio.ini").write_text(
        f"[env:testenv]\nplatform = espressif32\nboard = esp32dev\n{body}"
    )


def _library_json(lib_dir: Path, name: str, dependencies: Any) -> None:
    (lib_dir / "library.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "dependencies": dependencies})
    )


def _ublox(project: Path, make_registry_lib: Any) -> None:
    """The u-blox shape: ini ^3.1.7, board_support's library.json pins 3.1.13."""
    _ini(project, f"sparkfun/{UBLOX} @ ^3.1.7")
    make_registry_lib("testenv", UBLOX, "3.1.15", "sparkfun", project_dir=project)
    make_registry_lib(
        "testenv", UBLOX, "3.1.13", "sparkfun", project_dir=project, dir_name=f"{UBLOX}@3.1.13"
    )
    board = project / "lib" / "board_support"
    board.mkdir(parents=True)
    _library_json(board, "board_support", {UBLOX: "3.1.13"})


def test_registry_duplicates_detected(tmp_project, make_registry_lib, mock_git_commands):
    make_registry_lib("testenv", "X", "3.1.15", project_dir=tmp_project)
    make_registry_lib("testenv", "X", "3.1.13", project_dir=tmp_project, dir_name="X@3.1.13")
    make_registry_lib("testenv", "Other", "1.0.0", project_dir=tmp_project)

    groups = pio_lock.find_duplicate_libraries(tmp_project, "testenv")

    assert [[c.dir for c in group] for group in groups] == [["X", "X@3.1.13"]]
    assert [c.version for c in groups[0]] == ["3.1.15", "3.1.13"]
    assert {c.type for c in groups[0]} == {"registry"}


def test_no_duplicates_is_empty(tmp_project, make_registry_lib, mock_git_commands):
    make_registry_lib("testenv", "X", "3.1.15", project_dir=tmp_project)
    make_registry_lib("testenv", "Y", "1.0.0", project_dir=tmp_project)
    assert pio_lock.find_duplicate_libraries(tmp_project, "testenv") == []
    assert pio_lock.find_duplicate_libraries(tmp_project, "missing-env") == []


def test_git_duplicates_detected_by_url(tmp_project, make_git_lib, mock_git_commands):
    url = "https://github.com/acme/embedded-bridge.git"
    make_git_lib("testenv", "embedded-bridge", sha="a" * 40, url=url, project_dir=tmp_project)
    make_git_lib(
        "testenv", "embedded-bridge@src-6d4c", sha="a" * 40, url=url + "/",
        project_dir=tmp_project,
    )  # fmt: skip
    make_git_lib(
        "testenv", "embedded-bridge@src-d6f0", sha="b" * 40,
        url="git+https://github.com/ACME/embedded-bridge", project_dir=tmp_project,
    )  # fmt: skip

    groups = pio_lock.find_duplicate_libraries(tmp_project, "testenv")

    assert [[c.dir for c in group] for group in groups] == [
        ["embedded-bridge", "embedded-bridge@src-6d4c", "embedded-bridge@src-d6f0"]
    ]
    assert [c.sha for c in groups[0]] == ["a" * 40, "a" * 40, "b" * 40]
    assert {c.name for c in groups[0]} == {"embedded-bridge"}


def test_registry_and_git_same_name_is_duplicate(
    tmp_project, make_registry_lib, make_git_lib, mock_git_commands
):
    make_registry_lib("testenv", "X", "1.2.0", project_dir=tmp_project)
    make_git_lib(
        "testenv", "x-fork@src-abc", sha="c" * 40, url="https://github.com/me/x-fork.git",
        project_dir=tmp_project, library_json_name="x",
    )  # fmt: skip

    groups = pio_lock.find_duplicate_libraries(tmp_project, "testenv")

    assert [[(c.dir, c.type) for c in group] for group in groups] == [
        [("X", "registry"), ("x-fork@src-abc", "git")]
    ]


def test_requested_by_lists_lib_deps_and_library_json(
    tmp_project, make_registry_lib, mock_git_commands
):
    _ini(tmp_project, "sparkfun/X @ ^3.1.7")
    make_registry_lib("testenv", "X", "3.1.15", project_dir=tmp_project)
    make_registry_lib("testenv", "X", "3.1.13", project_dir=tmp_project, dir_name="X@3.1.13")
    user = make_registry_lib("testenv", "UsesX", "2.0.0", project_dir=tmp_project)
    _library_json(user, "UsesX", [{"owner": "sparkfun", "name": "X", "version": "3.1.13"}])

    (group,) = pio_lock.find_duplicate_libraries(tmp_project, "testenv")
    by_dir = {c.dir: c.requested_by for c in group}

    assert by_dir == {
        "X": ["lib_deps: sparkfun/X @ ^3.1.7"],
        "X@3.1.13": ["UsesX: sparkfun/X@3.1.13"],
    }


def test_duplicate_problems_message(tmp_project, make_registry_lib, mock_git_commands):
    make_registry_lib("testenv", "X", "3.1.15", project_dir=tmp_project)
    make_registry_lib("testenv", "X", "3.1.13", project_dir=tmp_project, dir_name="X@3.1.13")

    messages = pio_lock.duplicate_problems(
        pio_lock.find_duplicate_libraries(tmp_project, "testenv")
    )

    assert messages == [
        "duplicate copies of 'X': X (3.1.15; requested by unknown), "
        "X@3.1.13 (3.1.13; requested by unknown); "
        "make the requirements agree so only one copy is installed"
    ]


def test_duplicate_report_points_at_exact_library_pin(
    tmp_project, make_registry_lib, mock_git_commands
):
    _ublox(tmp_project, make_registry_lib)

    (message,) = pio_lock.duplicate_problems(
        pio_lock.find_duplicate_libraries(tmp_project, "testenv")
    )

    assert message == (
        f"duplicate copies of '{UBLOX}': "
        f"{UBLOX} (3.1.15; requested by lib_deps: sparkfun/{UBLOX} @ ^3.1.7), "
        f"{UBLOX}@3.1.13 (3.1.13; requested by lib/board_support: {UBLOX}@3.1.13); "
        "make the requirements agree so only one copy is installed; "
        f"lib/board_support pins '{UBLOX}' to 3.1.13 — prefer a range (e.g. ^3.1.0) "
        "in library.json"
    )


def test_project_exact_pin_gets_no_library_advice(
    tmp_project, make_registry_lib, mock_git_commands
):
    _ini(tmp_project, "X @ 3.1.15")
    make_registry_lib("testenv", "X", "3.1.15", project_dir=tmp_project)
    make_registry_lib("testenv", "X", "3.1.13", project_dir=tmp_project, dir_name="X@3.1.13")

    (message,) = pio_lock.duplicate_problems(
        pio_lock.find_duplicate_libraries(tmp_project, "testenv")
    )
    assert "pins" not in message
    assert "X (3.1.15; requested by lib_deps: X @ 3.1.15)" in message


def test_git_sha_pin_in_library_json_is_exact(tmp_project, make_git_lib, mock_git_commands):
    url = "https://github.com/acme/bridge.git"
    _ini(tmp_project, f"{url}#main")
    make_git_lib("testenv", "bridge", sha="a" * 40, url=url, project_dir=tmp_project)
    make_git_lib(
        "testenv", "bridge@src-1", sha="d6f0" + "0" * 36, url=url, project_dir=tmp_project
    )
    app = tmp_project / "lib" / "app"
    app.mkdir(parents=True)
    _library_json(app, "app", {"bridge": f"{url}#d6f0000"})

    (group,) = pio_lock.find_duplicate_libraries(tmp_project, "testenv")
    assert {c.dir: c.requested_by for c in group} == {
        "bridge": [f"lib_deps: {url}#main"],
        "bridge@src-1": [f"lib/app: bridge@{url}#d6f0000"],
    }
    (message,) = pio_lock.duplicate_problems([group])
    assert "bridge (aaaaaaa; requested by" in message
    assert "lib/app pins 'bridge' to d6f0000 — prefer a range in library.json" in message


# ── scan / capture ───────────────────────────────────────────────────────────


def test_scan_records_dir_and_src_checkouts(tmp_project, make_git_lib, mock_git_commands):
    make_git_lib(
        "testenv", "bridge@src-6d4c", sha="a" * 40, url="https://github.com/acme/bridge.git",
        project_dir=tmp_project,
    )  # fmt: skip
    unmanaged = tmp_project / ".pio" / "libdeps" / "testenv" / "junk@src-ffff"
    unmanaged.mkdir()

    libs = pio_lock.scan_env_libraries(tmp_project, "testenv")

    assert libs == [
        {
            "name": "bridge",
            "type": "git",
            "url": "https://github.com/acme/bridge.git",
            "sha": "a" * 40,
            "dir": "bridge@src-6d4c",
        }
    ]


def test_capture_records_src_git_checkouts_with_dir(
    tmp_project, make_registry_lib, make_git_lib, fake_pio, capsys
):
    make_registry_lib("testenv", "X", "1.2.0", project_dir=tmp_project)
    make_git_lib(
        "testenv", "X@src-abc", sha="c" * 40, url="https://github.com/me/x.git",
        project_dir=tmp_project, library_json_name="X",
    )  # fmt: skip
    fake_pio({"testenv": espressif32_lock()})

    assert pio_lock.capture(tmp_project, ["testenv"]) == 0

    libs = json.loads((tmp_project / "pio.lock.json").read_text())["envs"]["testenv"]["libraries"]
    assert [(lib["dir"], lib["type"]) for lib in libs] == [("X", "registry"), ("X@src-abc", "git")]
    err = capsys.readouterr().err
    assert "Warning: duplicate copies of 'X': X (1.2.0; requested by unknown)" in err


# ── check ────────────────────────────────────────────────────────────────────


def _ublox_lock(project: Path) -> None:
    write_lockfile(
        project,
        {
            "testenv": [
                {"name": UBLOX, "type": "registry", "version": "3.1.15", "owner": "sparkfun"},
                {"name": UBLOX, "type": "registry", "version": "3.1.13", "owner": "sparkfun"},
            ]
        },
    )


def test_check_reports_duplicates_as_warning_not_misleading_drift(
    tmp_project, make_registry_lib, mock_git_commands, capsys, monkeypatch
):
    monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    _ublox(tmp_project, make_registry_lib)
    _ublox_lock(tmp_project)

    assert pio_lock.check(tmp_project, ["testenv"]) == 0
    out = capsys.readouterr().out
    assert f"WARNING: duplicate copies of '{UBLOX}'" in out
    assert "expected 3.1.13" not in out
    assert "DRIFT" not in out

    assert pio_lock.check(tmp_project, ["testenv"], warn_only=True) == 0
    assert f"WARNING: duplicate copies of '{UBLOX}'" in capsys.readouterr().out


def test_check_duplicates_do_not_hide_other_drift(
    tmp_project, make_registry_lib, mock_git_commands, capsys, monkeypatch
):
    monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    _ublox(tmp_project, make_registry_lib)
    make_registry_lib("testenv", "Other", "1.0.0", project_dir=tmp_project)
    write_lockfile(
        tmp_project,
        {
            "testenv": [
                {"name": UBLOX, "type": "registry", "version": "3.1.13", "owner": "sparkfun"},
                {"name": "Other", "type": "registry", "version": "2.0.0", "owner": "testowner"},
            ]
        },
    )
    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "DRIFT: Other — expected 2.0.0" in out
    assert "expected 3.1.13" not in out


def test_check_uses_dir_for_non_duplicates(tmp_project, make_git_lib, mock_git_commands, capsys):
    make_git_lib(
        "testenv", "bridge@src-d6f0", sha="b" * 40, url="https://github.com/acme/bridge.git",
        project_dir=tmp_project,
    )  # fmt: skip
    make_git_lib(
        "testenv", "other", sha="e" * 40, url="https://github.com/acme/other.git",
        project_dir=tmp_project,
    )  # fmt: skip
    lock_entry = {
        "name": "bridge", "type": "git", "url": "https://github.com/acme/bridge.git",
        "sha": "b" * 40, "dir": "bridge@src-d6f0",
    }  # fmt: skip
    assert pio_lock.is_installed_at_locked_version(tmp_project, "testenv", lock_entry)
    # With a dir, only that directory is compared (URL matching would find bridge@src-d6f0).
    elsewhere = {**lock_entry, "dir": "other"}
    assert not pio_lock.is_installed_at_locked_version(tmp_project, "testenv", elsewhere)

    write_lockfile(tmp_project, {"testenv": [lock_entry]})
    assert pio_lock.check(tmp_project, ["testenv"]) == 0


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_warns_on_duplicates(tmp_project, make_registry_lib, mock_git_commands, capsys):
    _ublox(tmp_project, make_registry_lib)
    _ublox_lock(tmp_project)

    assert pio_lock.restore(tmp_project, ["testenv"]) == 0
    captured = capsys.readouterr()
    assert f"Warning: duplicate copies of '{UBLOX}'" in captured.err
    assert "Restoring 2 libraries" in captured.out


def test_check_still_compares_a_duplicated_entry_with_its_dir(
    tmp_project, make_registry_lib, mock_git_commands, capsys, monkeypatch
):
    monkeypatch.delenv("PIO_LOCK_DRIFT", raising=False)
    make_registry_lib("testenv", "X", "3.1.16", project_dir=tmp_project)
    make_registry_lib("testenv", "X", "3.1.13", project_dir=tmp_project, dir_name="X@3.1.13")
    write_lockfile(
        tmp_project,
        {
            "testenv": [
                {
                    "name": "X", "type": "registry", "version": "3.1.15",
                    "owner": "testowner", "dir": "X",
                },
                {
                    "name": "X", "type": "registry", "version": "3.1.13",
                    "owner": "testowner", "dir": "X@3.1.13",
                },
            ]
        },
    )  # fmt: skip

    assert pio_lock.check(tmp_project, ["testenv"]) == 1
    out = capsys.readouterr().out
    assert "WARNING: duplicate copies of 'X'" in out
    assert "DRIFT: X — expected 3.1.15" in out
    assert "expected 3.1.13" not in out


def test_entry_with_missing_dir_falls_back_to_name_and_url(
    tmp_project, make_registry_lib, make_git_lib, mock_git_commands
):
    make_registry_lib("testenv", "X", "1.0.0", project_dir=tmp_project)
    make_git_lib(
        "testenv", "bridge", sha="b" * 40, url="https://github.com/acme/bridge.git",
        project_dir=tmp_project,
    )  # fmt: skip
    registry = {"name": "X", "type": "registry", "version": "1.0.0", "dir": "X@src-gone"}
    git = {
        "name": "bridge", "type": "git", "url": "https://github.com/acme/bridge.git",
        "sha": "b" * 40, "dir": "bridge@src-other-machine",
    }  # fmt: skip
    assert pio_lock.is_installed_at_locked_version(tmp_project, "testenv", registry)
    assert pio_lock.is_installed_at_locked_version(tmp_project, "testenv", git)
    assert not pio_lock.is_installed_at_locked_version(
        tmp_project, "testenv", {**registry, "version": "2.0.0"}
    )
