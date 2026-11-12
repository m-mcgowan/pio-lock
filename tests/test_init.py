"""Tests for ``pio-lock init`` (text edit of platformio.ini; no PlatformIO needed)."""

from __future__ import annotations

from pathlib import Path

import pio_lock


def _run(tmp_path: Path, text: str, dry_run: bool = False) -> tuple[int, str]:
    ini = tmp_path / "platformio.ini"
    ini.write_text(text)
    rc = pio_lock.init(tmp_path, dry_run=dry_run)
    return rc, ini.read_text()


def test_init_adds_platformio_section(tmp_path):
    rc, text = _run(tmp_path, "; my project\n[env:native]\nplatform = native\n")
    assert rc == 0
    assert text == (
        "[platformio]\nextra_configs = pio.lock.ini\n\n"
        "; my project\n[env:native]\nplatform = native\n"
    )


def test_init_adds_extra_configs_option(tmp_path):
    rc, text = _run(
        tmp_path,
        "[platformio]\ndefault_envs = native\n\n[env:native]\nplatform = native\n",
    )
    assert rc == 0
    assert text == (
        "[platformio]\nextra_configs = pio.lock.ini\ndefault_envs = native\n\n"
        "[env:native]\nplatform = native\n"
    )


def test_init_appends_to_single_line_extra_configs(tmp_path):
    rc, text = _run(
        tmp_path,
        "[platformio]\nextra_configs = other.ini\n\n[env:native]\nplatform = native\n",
    )
    assert rc == 0
    assert text == (
        "[platformio]\nextra_configs = other.ini\n    pio.lock.ini\n\n"
        "[env:native]\nplatform = native\n"
    )


def test_init_appends_to_multiline_extra_configs(tmp_path):
    rc, text = _run(
        tmp_path,
        "[platformio]\n"
        "extra_configs =\n"
        "  configs/*.ini\n"
        "  ; shared boards\n"
        "  boards.ini\n"
        "default_envs = native\n"
        "\n[env:native]\nplatform = native\n",
    )
    assert rc == 0
    assert text == (
        "[platformio]\n"
        "extra_configs =\n"
        "  configs/*.ini\n"
        "  ; shared boards\n"
        "  boards.ini\n"
        "  pio.lock.ini\n"
        "default_envs = native\n"
        "\n[env:native]\nplatform = native\n"
    )


def test_init_is_idempotent(tmp_path, capsys):
    rc, first = _run(tmp_path, "[env:native]\nplatform = native\n")
    assert rc == 0
    capsys.readouterr()
    assert pio_lock.init(tmp_path) == 0
    assert (tmp_path / "platformio.ini").read_text() == first
    assert "pio.lock.ini already included" in capsys.readouterr().out


def test_init_dry_run_writes_nothing(tmp_path, capsys):
    original = "[env:native]\nplatform = native\n"
    rc, text = _run(tmp_path, original, dry_run=True)
    assert rc == 0
    assert text == original
    assert "extra_configs = pio.lock.ini" in capsys.readouterr().out


def test_init_cli_does_not_need_platformio(tmp_path, monkeypatch):
    (tmp_path / "platformio.ini").write_text("[env:native]\nplatform = native\n")
    monkeypatch.setattr(
        pio_lock, "_ensure_pio_python", lambda argv: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr("sys.argv", ["pio-lock", "-d", str(tmp_path), "init"])
    assert pio_lock.main() == 0
    assert "extra_configs = pio.lock.ini" in (tmp_path / "platformio.ini").read_text()


def test_init_reads_and_writes_utf8_whatever_the_locale(tmp_path):
    """platformio.ini is UTF-8; an ASCII locale must not break init or mangle the text."""
    import subprocess
    import sys

    ini = tmp_path / "platformio.ini"
    ini.write_text("; café — münchen\n[env:native]\nplatform = native\n", encoding="utf-8")
    repo = Path(pio_lock.__file__).resolve().parent
    code = (
        "import sys, pio_lock; from pathlib import Path; "
        "sys.exit(pio_lock.init(Path(sys.argv[1])))"
    )
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
           "PYTHONUTF8": "0", "PYTHONPATH": str(repo)}  # fmt: skip
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert ini.read_text(encoding="utf-8") == (
        "[platformio]\nextra_configs = pio.lock.ini\n\n"
        "; café — münchen\n[env:native]\nplatform = native\n"
    )
