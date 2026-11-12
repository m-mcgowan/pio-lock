#!/usr/bin/env python3
"""
pio-lock — PlatformIO dependency lockfile tool.

Captures resolved dependency versions into a lockfile (pio.lock.json) for
reproducible builds. Works alongside platformio.ini, which declares desired
version ranges; this tool records the exact versions that were actually installed.

Can be used as:
  1. Standalone CLI:  python pio_lock.py capture -e myenv
  2. PIO extra_script: adds "lock-capture", "lock-check", "lock-restore" targets

Requires: Python 3.8+, git CLI, PlatformIO Core CLI
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import glob
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LOCKFILE_NAME = "pio.lock.json"
LOCK_INI_NAME = "pio.lock.ini"
SNAPSHOT_NAME = "build_snapshot.json"

LOCKFILE_VERSION = 2


# ── File helpers ─────────────────────────────────────────────────────────────


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically (see ``_atomic_write_text``)."""
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: temp file in the same dir, fsync, rename.

    On any failure the temp file is removed and the original ``path`` is left
    untouched, so an interrupted write can never leave a half-written file.
    """
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# ── Shell helpers (mockable seam) ────────────────────────────────────────────


def _default_run_cmd(args: list[str], cwd: Optional[str] = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        check=check,
    )
    return result.stdout.strip()


# Module-level runner — tests replace this to avoid real subprocesses.
_run_cmd: Callable[..., str] = _default_run_cmd


def run_cmd(args: list[str], cwd: Optional[str | Path] = None, check: bool = True) -> str:
    """Run a command and return stdout, stripped."""
    return _run_cmd(args, cwd=str(cwd) if cwd else None, check=check)


# ── Git helpers ──────────────────────────────────────────────────────────────


def get_git_sha(path: str | Path) -> str:
    return run_cmd(["git", "rev-parse", "HEAD"], cwd=str(path))


def get_git_remote_url(path: str | Path) -> str:
    return run_cmd(["git", "remote", "get-url", "origin"], cwd=str(path), check=False)


def get_git_commit(project_dir: Path) -> str:
    try:
        return run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=str(project_dir))
    except subprocess.CalledProcessError:
        return "unknown"


def is_git_dirty(project_dir: Path) -> bool:
    """Check if tracked files have uncommitted changes.

    Untracked files are not in the build's source as committed, so they do not make it dirty.
    """
    try:
        status = run_cmd(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(project_dir),
            check=False,
        )
        return len(status) > 0
    except subprocess.CalledProcessError:
        return False


def get_commit_epoch(project_dir: Path) -> Optional[int]:
    """The HEAD commit's committer time (Unix seconds), or None outside git."""
    try:
        return int(run_cmd(["git", "log", "-1", "--format=%ct"], cwd=str(project_dir)))
    except (subprocess.CalledProcessError, ValueError):
        return None


# ── Build snapshot ───────────────────────────────────────────────────────────


def load_snapshot(project_dir: Path) -> Optional[dict[str, Any]]:
    """Load an existing build snapshot, or return None."""
    path = project_dir / SNAPSHOT_NAME
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Failed to read {SNAPSHOT_NAME}: {e}", file=sys.stderr)
        return None


def generate_snapshot(
    project_dir: Path, *, pinned: bool = False, write: bool = True
) -> dict[str, Any]:
    """Generate a new build snapshot from the current state.

    With ``write=False`` the snapshot is computed but not written to disk.
    """
    commit = get_git_commit(project_dir)
    dirty = is_git_dirty(project_dir)
    commit_epoch = None if dirty else get_commit_epoch(project_dir)
    if commit_epoch is not None:
        # The same commit gives the same epoch on every machine, so __DATE__/__TIME__ and the
        # bootloader's description block match between a local build and CI's.
        epoch, epoch_source = commit_epoch, "commit"
    else:
        epoch, epoch_source = int(time.time()), "time"
        reason = "dirty tree" if dirty else "no commit time"
        print(
            f"WARNING: non-reproducible: {reason}; SOURCE_DATE_EPOCH is the current time",
            file=sys.stderr,
        )

    snapshot: dict[str, Any] = {
        "source_date_epoch": epoch,
        "epoch_source": epoch_source,
        "build_date": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "pinned": pinned,
    }

    if dirty:
        print("WARNING: Working tree has uncommitted changes", file=sys.stderr)

    if write:
        _atomic_write_json(project_dir / SNAPSHOT_NAME, snapshot)
    dirty_marker = "*" if dirty else ""
    print(f"Build snapshot generated: {snapshot['build_date']} (commit {commit}{dirty_marker})")
    return snapshot


def is_snapshot_stale(snapshot: dict[str, Any], project_dir: Path) -> bool:
    """True when the snapshot is for another commit, or its epoch source no longer fits the tree.

    A clean tree wants the commit's time; a dirty one keeps one wall-clock time until it is clean
    again (so a dirty incremental build does not re-stamp every build). A snapshot without
    epoch_source was written before the epoch came from the commit: treat it as wall-clock.
    """
    head = get_git_commit(project_dir)
    if head == "unknown":
        return False
    if snapshot.get("git_commit") != head:
        return True
    wanted = "time" if is_git_dirty(project_dir) else "commit"
    return bool(snapshot.get("epoch_source", "time") != wanted)


def apply_snapshot(snapshot: dict[str, Any]) -> None:
    """Set SOURCE_DATE_EPOCH from the snapshot.

    This overrides any SOURCE_DATE_EPOCH already in the environment: the snapshot is the
    build's single source for the epoch.
    """
    epoch = str(snapshot["source_date_epoch"])
    os.environ["SOURCE_DATE_EPOCH"] = epoch
    build_date = snapshot.get("build_date", "unknown")
    commit = snapshot.get("git_commit", "unknown")
    pinned = snapshot.get("pinned", False)
    tag = " [pinned]" if pinned else ""
    print(f"Build snapshot: SOURCE_DATE_EPOCH={epoch} ({build_date}, commit {commit}{tag})")


LOCK_TARGETS = frozenset(
    {
        "lock-capture",
        "lock-check",
        "lock-restore",
        "snapshot-capture",
        "snapshot-check",
        "snapshot-clear",
        "snapshot-print",
    }
)


def _requested_targets(env: Any) -> set[str]:
    """Return the targets requested on the command line (empty if unavailable)."""
    try:
        from SCons.Script import COMMAND_LINE_TARGETS  # type: ignore[import-not-found]

        return {str(t) for t in COMMAND_LINE_TARGETS}
    except ImportError:
        return set()


def _is_lock_target_run(targets: set[str]) -> bool:
    """True if any requested target is one of pio-lock's own targets."""
    return bool(targets & LOCK_TARGETS)


def snapshot_auto_manage(project_dir: Path, env: Any = None, *, checks: bool = True) -> None:
    """Auto-manage the snapshot during builds.

    Called from the PIO extra_script hook. Sets SOURCE_DATE_EPOCH so that
    __DATE__ and __TIME__ macros are deterministic across rebuilds.

    With ``checks=False`` (a lock/snapshot target was requested) a missing or stale
    snapshot never aborts the run: it is regenerated and applied instead.
    """
    snapshot = load_snapshot(project_dir)
    release = _is_release_mode(snapshot, env)

    if not checks:
        # A lock/snapshot target is running: never touch build_snapshot.json, so the target
        # sees the real state. Apply what exists, or an in-memory snapshot if none does.
        if snapshot is None:
            snapshot = generate_snapshot(project_dir, write=False)
        apply_snapshot(snapshot)
        return

    if snapshot is None:
        if release:
            print(
                f"ERROR: No {SNAPSHOT_NAME} in release mode. Run: pio run -t snapshot-capture",
                file=sys.stderr,
            )
            if env:
                env.Exit(1)
            return
        snapshot = generate_snapshot(project_dir)
    elif snapshot.get("pinned", False):
        # Pinned — never auto-regenerate
        pass
    elif is_snapshot_stale(snapshot, project_dir):
        if release:
            head = get_git_commit(project_dir)
            if snapshot.get("git_commit") == head:
                tree = "dirty" if is_git_dirty(project_dir) else "clean"
                detail = (
                    f"the snapshot's epoch source is {snapshot.get('epoch_source', 'time')}; "
                    f"the tree is {tree}: rerun snapshot-capture"
                )
            else:
                detail = f"Snapshot commit: {snapshot.get('git_commit')}, HEAD: {head}"
            print(
                f"ERROR: {SNAPSHOT_NAME} is stale in release mode. {detail}",
                file=sys.stderr,
            )
            if env:
                env.Exit(1)
            return
        head = get_git_commit(project_dir)
        print(
            f"Build snapshot stale (was {snapshot.get('git_commit')}, "
            f"HEAD is {head}), regenerating..."
        )
        snapshot = generate_snapshot(project_dir)

    apply_snapshot(snapshot)


def _is_release_mode(snapshot: Optional[dict[str, Any]], env: Any = None) -> bool:
    """Check if any release mode indicator is active."""
    if snapshot and snapshot.get("pinned", False):
        return True
    if os.environ.get("PIO_RELEASE_BUILD", "").strip() == "1":
        return True
    if env is not None:
        try:
            val = env.GetProjectOption("custom_reproducible_release", "false")
            if val.strip().lower() == "true":
                return True
        except Exception:
            pass
    return False


def snapshot_capture(project_dir: Path) -> int:
    """Generate/overwrite a build snapshot from current HEAD."""
    generate_snapshot(project_dir)
    return 0


def snapshot_check(project_dir: Path) -> int:
    """Verify snapshot matches HEAD. Returns non-zero if stale/missing."""
    snapshot = load_snapshot(project_dir)
    if snapshot is None:
        print("No build snapshot found.")
        return 1

    head = get_git_commit(project_dir)
    commit = snapshot.get("git_commit")
    pinned = snapshot.get("pinned", False)
    stale = is_snapshot_stale(snapshot, project_dir)

    print(f"Snapshot: {snapshot.get('build_date', 'unknown')}")
    print(f"  Commit:  {commit} {'(pinned)' if pinned else ''}")
    print(f"  HEAD:    {head}")
    print(f"  Dirty:   {snapshot.get('git_dirty', 'unknown')}")
    print(f"  Status:  {'STALE' if stale else 'FRESH'}")

    return 1 if stale else 0


def snapshot_clear(project_dir: Path) -> int:
    """Delete the build snapshot file."""
    path = project_dir / SNAPSHOT_NAME
    if path.exists():
        path.unlink()
        print(f"Build snapshot removed: {path}")
    else:
        print("No build snapshot to remove.")
    return 0


def snapshot_print(project_dir: Path) -> int:
    """Print the build snapshot contents."""
    snapshot = load_snapshot(project_dir)
    if snapshot is None:
        print("No build snapshot found.")
        return 1
    print(json.dumps(snapshot, indent=2))
    return 0


# ── PIO helpers ──────────────────────────────────────────────────────────────


def get_pio_core_version() -> str:
    try:
        output = run_cmd(["pio", "system", "info"])
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    for line in output.splitlines():
        if "PlatformIO Core" in line:
            parts = line.split()
            return parts[-1]
    return "unknown"


def get_platform_url(project_dir: Path, env_name: str) -> str:
    try:
        output = run_cmd(["pio", "project", "config", "--json-output"], cwd=str(project_dir))
        sections = json.loads(output)
        for section in sections:
            if section[0] == f"env:{env_name}":
                for key, val in section[1]:
                    if key == "platform":
                        return str(val)
        for section in sections:
            if section[0] == "env":
                for key, val in section[1]:
                    if key == "platform":
                        return str(val)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        pass
    return "unknown"


# ── Scanners ─────────────────────────────────────────────────────────────────


def scan_env_libraries(project_dir: Path, env_name: str) -> list[dict[str, Any]]:
    """Scan .pio/libdeps/<env>/ for installed library versions."""
    libdeps_dir = project_dir / ".pio" / "libdeps" / env_name
    libraries: list[dict[str, Any]] = []

    if not libdeps_dir.is_dir():
        print(f"  Warning: {libdeps_dir} does not exist", file=sys.stderr)
        return libraries

    for lib_dir in sorted(libdeps_dir.iterdir()):
        if not lib_dir.is_dir():
            continue

        name = lib_dir.name

        # Skip PIO shadow copies (e.g. NimBLE-Arduino@src-abc123)
        if "@src-" in name:
            continue

        entry = _scan_single_library(lib_dir, name)
        if entry is not None:
            libraries.append(entry)

    return libraries


def _scan_single_library(lib_dir: Path, name: str) -> Optional[dict[str, Any]]:
    """Scan a single library directory and return its lockfile entry."""
    piopm_path = lib_dir / ".piopm"

    if piopm_path.exists():
        try:
            meta = json.loads(piopm_path.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: corrupt .piopm in {name}", file=sys.stderr)
            return None

        spec = meta.get("spec", {})
        uri = spec.get("uri") or ""

        if uri.startswith("file://"):
            return {
                "name": meta.get("name", name),
                "type": "local",
                "path": uri,
            }
        else:
            entry: dict[str, Any] = {
                "name": meta.get("name", name),
                "type": "registry",
                "version": meta.get("version", "unknown"),
            }
            owner = spec.get("owner")
            if owner:
                entry["owner"] = owner
            return entry

    elif (lib_dir / ".git").exists() or (lib_dir / ".git").is_file():
        sha = get_git_sha(lib_dir)
        url = get_git_remote_url(lib_dir)

        display_name = name
        lib_json_path = lib_dir / "library.json"
        if lib_json_path.exists():
            try:
                lib_data = json.loads(lib_json_path.read_text())
                display_name = lib_data.get("name", name)
            except json.JSONDecodeError:
                pass

        return {
            "name": display_name,
            "type": "git",
            "url": url,
            "sha": sha,
        }
    else:
        print(
            f"  Warning: {name} has no .piopm and no .git — skipping",
            file=sys.stderr,
        )
        return None


# ── Version check helpers ────────────────────────────────────────────────────


def is_installed_at_locked_version(
    project_dir: Path, env_name: str, lib_entry: dict[str, Any]
) -> bool:
    """Check if a library is already installed at the locked version."""
    libdeps_dir = project_dir / ".pio" / "libdeps" / env_name

    if not libdeps_dir.is_dir():
        return False

    if lib_entry["type"] == "registry":
        for lib_dir in libdeps_dir.iterdir():
            if not lib_dir.is_dir():
                continue
            piopm = lib_dir / ".piopm"
            if piopm.exists():
                try:
                    meta = json.loads(piopm.read_text())
                    if meta.get("name") == lib_entry["name"]:
                        return bool(meta.get("version") == lib_entry["version"])
                except json.JSONDecodeError:
                    pass
        return False

    elif lib_entry["type"] == "git":
        for lib_dir in libdeps_dir.iterdir():
            if not lib_dir.is_dir():
                continue
            if not ((lib_dir / ".git").exists() or (lib_dir / ".git").is_file()):
                continue
            try:
                url = get_git_remote_url(lib_dir)
                if url == lib_entry["url"]:
                    sha = get_git_sha(lib_dir)
                    return bool(sha == lib_entry["sha"])
            except subprocess.CalledProcessError:
                pass
        return False

    return True  # local libs are always "installed"


# ── Commands ─────────────────────────────────────────────────────────────────


def load_lockfile(path: Path) -> dict[str, Any]:
    """Read a lockfile; files written before format v2 get ``lockfile_version: 1``."""
    data: dict[str, Any] = json.loads(Path(path).read_text())
    data.setdefault("lockfile_version", 1)
    return data


_GIT_SHA_SUFFIX_RE = re.compile(r"\+sha\.([0-9a-f]+)$")


def _pinned_src_url(src_url: str, version: str) -> str:
    """``src_url`` pinned to the resolved commit for git sources.

    PlatformIO records the requested URI (which may name a branch or no ref at all) and puts
    the resolved commit in the version's ``+sha.<rev>`` suffix. Other URLs are returned as-is.
    """
    if not src_url.startswith("git+"):
        return src_url
    match = _GIT_SHA_SUFFIX_RE.search(version)
    if not match:
        print(
            f"  Warning: no resolved commit recorded for {src_url}; {LOCK_INI_NAME} can't pin it",
            file=sys.stderr,
        )
        return src_url
    return f"{src_url.split('#', 1)[0]}#{match.group(1)}"


def _platform_exact_spec(platform: dict[str, Any]) -> str:
    if platform.get("src_url"):
        return _pinned_src_url(str(platform["src_url"]), str(platform["version"]))
    owner = platform.get("owner") or "platformio"
    return f"{owner}/{platform['name']}@{platform['version']}"


def _package_exact_spec(package: dict[str, Any]) -> Optional[str]:
    """The ``platform_packages`` entry pinning ``package``; None for machine-local packages."""
    src_url = package.get("src_url")
    if src_url:
        if str(src_url).startswith("file://"):
            return None  # a local path in a committed file would break other machines
        # The name keeps PlatformIO treating it as an override of the platform's package.
        return f"{package['name']} @ {_pinned_src_url(str(src_url), str(package['version']))}"
    owner = package.get("owner")
    prefix = f"{owner}/" if owner else ""
    return f"{prefix}{package['name']}@{package['version']}"


_LOCK_INI_HEADER = f"""\
# Generated by pio-lock from {LOCKFILE_NAME}. Do not edit: run `pio-lock capture` instead.
#
# Pins each env's platform and platform packages to the versions recorded in the lock.
# Takes effect only when included from platformio.ini:
#
#   [platformio]
#   extra_configs = {LOCK_INI_NAME}
#
# To disable, remove {LOCK_INI_NAME} from extra_configs.
"""


def render_lock_ini(lockdata: dict[str, Any]) -> str:
    """Render ``pio.lock.ini``: each locked env's exact platform and platform packages."""
    lines = [_LOCK_INI_HEADER]
    for env_name in sorted(lockdata.get("envs", {})):
        platform = lockdata["envs"][env_name].get("platform")
        if not platform:
            continue
        lines.append(f"\n[env:{env_name}]\nplatform = {_platform_exact_spec(platform)}\n")
        specs = [_package_exact_spec(pkg) for pkg in platform.get("packages", [])]
        specs = [spec for spec in specs if spec]
        if specs:
            lines.append("platform_packages =\n")
            lines.extend(f"  {spec}\n" for spec in specs)
    return "".join(lines)


def _lock_staleness(
    platform: dict[str, Any], user_platform_spec: str, user_platform_packages: list[str]
) -> list[str]:
    """Why a locked platform no longer matches the user's config (empty when it matches)."""
    problems: list[str] = []
    if platform.get("spec") != user_platform_spec:
        problems.append(
            f"platform spec changed: '{platform.get('spec')}' → '{user_platform_spec}'; "
            "run capture"
        )
    locked_packages = list(platform.get("platform_packages_spec", []))
    if locked_packages != list(user_platform_packages):
        problems.append(
            f"platform_packages changed: {locked_packages} → {list(user_platform_packages)}; "
            "run capture"
        )
    return problems


def env_lock_is_stale(
    env_lock: dict[str, Any], user_platform_spec: str, user_platform_packages: list[str]
) -> bool:
    """Whether the user's ``platform``/``platform_packages`` changed since the env was locked.

    An env lock without a platform (v1, or captured without PlatformIO) counts as stale.
    """
    platform = env_lock.get("platform")
    if not platform:
        return True
    return bool(_lock_staleness(platform, user_platform_spec, user_platform_packages))


_LIB_DEP_URL_RE = re.compile(
    r"(?:git\+)?([A-Za-z][A-Za-z0-9+.-]*://[^\s#]+|git@[^\s#]+)(?:#(\S+))?"
)


def _normalize_git_url(url: str) -> str:
    """Compare-key for a git URL: no ``git+`` prefix, trailing ``/`` or ``.git``; case-folded."""
    url = url.strip()
    if url.startswith("git+"):
        url = url[4:]
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/").lower()


def _annotate_git_refs(libraries: list[dict[str, Any]], lib_deps: Any) -> None:
    """Set ``ref``/``ref_type`` on git entries from the matching ``lib_deps`` spec (by URL)."""
    refs: dict[str, Optional[str]] = {}
    for spec in lib_deps or []:
        match = _LIB_DEP_URL_RE.search(str(spec))
        if match:
            refs.setdefault(_normalize_git_url(match.group(1)), match.group(2))
    for lib in libraries:
        if lib.get("type") != "git" or not lib.get("url"):
            continue
        ref = refs.get(_normalize_git_url(str(lib["url"])))
        lib["ref"] = ref
        lib["ref_type"] = _classify_git_ref(ref)


def capture(
    project_dir: Path,
    envs: list[str],
    output_path: Optional[Path] = None,
) -> int:
    """Lock each env's platform, platform packages and libraries (lockfile + pio.lock.ini).

    Records what is installed (no network, no installs). Entries for envs not captured are
    kept; envs no longer in the project config are pruned.
    """
    if not (project_dir / "platformio.ini").exists():
        print(f"Error: no platformio.ini in {project_dir}", file=sys.stderr)
        return 1

    adapter = _pio_adapter_factory()
    try:
        config: Any = adapter.user_config(project_dir)
        configured: Optional[set[str]] = set(config.envs())
    except ProjectConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except PioIncompatible:
        config, configured = None, None  # can't read the config: no validation, no pruning
    if config is not None and configured is not None:
        for env_name in envs:
            if env_name not in configured:
                print(f"Error: env '{env_name}' not found in platformio.ini", file=sys.stderr)
                return 1
            if not config.get(f"env:{env_name}", "platform", None):
                print(
                    f"Error: env '{env_name}' has no platform in platformio.ini", file=sys.stderr
                )
                return 1

    print(f"Capturing dependency state for: {', '.join(envs)}")
    dest = output_path or (project_dir / LOCKFILE_NAME)
    previous = load_lockfile(dest).get("envs", {}) if dest.exists() else {}

    captured: dict[str, dict[str, Any]] = {}
    for env_name in envs:
        print(f"  Scanning {env_name}...")
        env_lock: dict[str, Any] = {}
        try:
            env_lock["platform"] = asdict(adapter.resolve_platform(project_dir, env_name))
            print(f"    Platform {env_lock['platform']['name']} {env_lock['platform']['version']}")
        except PioIncompatible as exc:
            print(f"  Warning: {exc}", file=sys.stderr)
            previous_platform = previous.get(env_name, {}).get("platform")
            if previous_platform:
                env_lock["platform"] = previous_platform
                print(
                    f"  Warning: platform for env '{env_name}' not re-captured "
                    "(PlatformIO incompatible); keeping the previous lock",
                    file=sys.stderr,
                )
            else:
                print(f"  Warning: platform not locked for {env_name}", file=sys.stderr)
        except (PlatformNotInstalled, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        libraries = [
            lib for lib in scan_env_libraries(project_dir, env_name) if lib["name"] != "pio-lock"
        ]
        lib_deps = config.get(f"env:{env_name}", "lib_deps", []) if config is not None else []
        _annotate_git_refs(libraries, lib_deps)
        env_lock["libraries"] = libraries
        captured[env_name] = env_lock
        print(f"    Found {len(libraries)} libraries")

    merged: dict[str, Any] = {**previous, **captured}
    if configured is not None:
        for env_name in sorted(set(merged) - configured - set(captured)):
            del merged[env_name]
            print(f"Pruned env '{env_name}'")

    core_version = adapter.core_version()
    lockdata: dict[str, Any] = {
        "_comment": "Generated by pio-lock. Do not edit manually.",
        "lockfile_version": LOCKFILE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from_commit": get_git_commit(project_dir),
        "pio_core_version": core_version if core_version != "unknown" else get_pio_core_version(),
        "envs": {name: merged[name] for name in sorted(merged)},
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(dest, lockdata)
    print(f"Wrote {dest}")
    ini_path = dest.parent / LOCK_INI_NAME
    _atomic_write_text(ini_path, render_lock_ini(lockdata))
    print(f"Wrote {ini_path}")
    return 0


def _restore_platform(project_dir: Path, env_name: str, platform: dict[str, Any]) -> int:
    """Install the locked platform, then its locked packages, for one env."""
    print(f"Restoring platform {platform['name']} {platform['version']} for {env_name}...")
    installs = [("--platform", _platform_exact_spec(platform))]
    for pkg in platform.get("packages", []):
        spec = _package_exact_spec(pkg)
        if spec is None:
            print(f"  Package {pkg['name']} is a local tool managed by the platform; skipped")
            continue
        installs.append(("--tool", spec))
    for flag, spec in installs:
        print(f"  Installing: {spec}")
        try:
            run_cmd(
                [
                    "pio",
                    "pkg",
                    "install",
                    "-d",
                    str(project_dir),
                    "-e",
                    env_name,
                    flag,
                    spec,
                    "--no-save",
                ],
                cwd=str(project_dir),
            )
        except subprocess.CalledProcessError as e:
            print(f"  Error installing {spec}: {e}", file=sys.stderr)
            return 1
    return 0


def restore(project_dir: Path, envs: list[str]) -> int:
    """Install exact versions from the lockfile."""
    lockfile_path = project_dir / LOCKFILE_NAME
    if not lockfile_path.exists():
        print(f"Error: no {LOCKFILE_NAME} in {project_dir}", file=sys.stderr)
        return 1

    lockdata = load_lockfile(lockfile_path)

    for env_name in envs:
        if env_name not in lockdata.get("envs", {}):
            print(f"Error: env '{env_name}' not in lockfile", file=sys.stderr)
            return 1

        platform = lockdata["envs"][env_name].get("platform")
        if platform:
            if _restore_platform(project_dir, env_name, platform) != 0:
                return 1
        else:
            print(f"  Platform for {env_name}: env has no locked platform; run capture")

        libraries = lockdata["envs"][env_name]["libraries"]
        print(f"Restoring {len(libraries)} libraries for {env_name}...")

        skip_count = 0
        install_count = 0

        for lib in libraries:
            if lib["type"] == "local":
                continue

            if is_installed_at_locked_version(project_dir, env_name, lib):
                skip_count += 1
                continue

            spec = _lib_install_spec(lib)
            if spec is None:
                continue

            print(f"  Installing: {lib['name']}")
            try:
                run_cmd(
                    [
                        "pio",
                        "pkg",
                        "install",
                        "-d",
                        str(project_dir),
                        "-e",
                        env_name,
                        "--library",
                        spec,
                        "--no-save",
                    ],
                    cwd=str(project_dir),
                )
                install_count += 1
            except subprocess.CalledProcessError as e:
                print(f"  Error installing {lib['name']}: {e}", file=sys.stderr)
                return 1

        print(f"  Done: {install_count} installed, {skip_count} already up-to-date")

    return 0


_DRIFT_ENV_VAR = "PIO_LOCK_DRIFT"


def _drift_policy_from_env() -> Optional[str]:
    """``"warn"`` or ``"fail"`` from ``PIO_LOCK_DRIFT``; None when unset or unrecognised."""
    value = os.environ.get(_DRIFT_ENV_VAR, "").strip().lower()
    return value if value in ("warn", "fail") else None


def platform_drift(
    project_dir: Path, env: str, env_lock: dict[str, Any], adapter: PioAdapter
) -> list[str]:
    """Human-readable differences between the locked platform and what's configured/installed.

    Empty when the env lock has no platform, or when everything matches. May raise
    PioIncompatible (the caller decides how to report that) or ValueError (bad config).
    """
    platform = env_lock.get("platform")
    if not platform:
        return []

    config = adapter.user_config(project_dir)
    section = f"env:{env}"
    try:
        spec = str(config.get(section, "platform", "") or "")
        user_packages = [str(p) for p in config.get(section, "platform_packages", []) or []]
    except (AttributeError, TypeError) as exc:
        raise PioIncompatible(
            "platformio.project.config.ProjectConfig.get", adapter.core_version(), _describe(exc)
        ) from exc
    problems = _lock_staleness(platform, spec, user_packages)

    try:
        name, version = adapter.platform_in_use(project_dir, env)
    except PlatformNotInstalled:
        problems.append(
            f"platform {platform['name']} {platform['version']} is not installed; run restore"
        )
        return problems
    if version != platform["version"]:
        problems.append(f"platform {name}: locked {platform['version']}, in use {version}")
    for pkg in platform.get("packages", []):
        if not adapter.package_installed(pkg, project_dir):
            problems.append(f"package {pkg['name']}: locked {pkg['version']} is not installed")
    return problems


_ALLOW_OVERRIDE_ENV_VAR = "PIO_LOCK_ALLOW_OVERRIDE"
_LOCKED_OPTIONS = ("platform", "platform_packages")


def _allow_override_from_env() -> Optional[bool]:
    """``PIO_LOCK_ALLOW_OVERRIDE`` as a bool; None when unset or unrecognised."""
    value = os.environ.get(_ALLOW_OVERRIDE_ENV_VAR, "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


def _lock_ini_position(project_dir: Path, files: list[Path]) -> Optional[int]:
    """Index of pio.lock.ini among the config files PlatformIO reads (None: not included)."""
    lock_ini = os.path.realpath(Path(project_dir) / LOCK_INI_NAME)
    for i, path in enumerate(files):
        if os.path.realpath(path) == lock_ini:
            return i
    return None


def lock_overrides(project_dir: Path, env: str, adapter: PioAdapter) -> list[str]:
    """One message per config file read after pio.lock.ini that sets a locked option for ``env``.

    Files are taken in PlatformIO's read order (``adapter.config_files``). Only ``[env:<env>]``
    counts: a later ``[env]`` value does not override pio.lock.ini's ``[env:<env>]``. Empty when
    pio.lock.ini is not included. May raise PioIncompatible or ValueError like the adapter.
    """
    project_dir = Path(project_dir)
    files = adapter.config_files(project_dir)
    position = _lock_ini_position(project_dir, files)
    if position is None:
        return []
    messages: list[str] = []
    for path in files[position + 1 :]:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error:
            continue  # PlatformIO reports a broken config itself
        section = f"env:{env}"
        options = [o for o in _LOCKED_OPTIONS if parser.has_option(section, o)]
        if not options:
            continue
        try:
            name = Path(os.path.relpath(path, project_dir)).as_posix()
        except ValueError:  # different drive on Windows
            name = str(path)
        messages.append(
            f"{name} sets {', '.join(options)} for env '{env}' after {LOCK_INI_NAME}, "
            "overriding the lock"
        )
    return messages


def check(project_dir: Path, envs: list[str], warn_only: bool = False) -> int:
    """Verify installed state matches the lockfile.

    With ``warn_only`` (or ``PIO_LOCK_DRIFT=warn``) problems print as ``WARNING:`` and the
    exit status is 0.
    """
    lockfile_path = project_dir / LOCKFILE_NAME
    if not lockfile_path.exists():
        print(f"Error: no {LOCKFILE_NAME} in {project_dir}", file=sys.stderr)
        return 1

    lockdata = load_lockfile(lockfile_path)
    warn = warn_only or _drift_policy_from_env() == "warn"
    label = "WARNING" if warn else "DRIFT"
    problems_found = False
    adapter: Optional[PioAdapter] = None
    platform_checked = False

    if lockdata["lockfile_version"] < 2:
        print("platform not checked: lockfile is v1; run capture to lock the platform")

    for env_name in envs:
        if env_name not in lockdata.get("envs", {}):
            print(f"Error: env '{env_name}' not in lockfile", file=sys.stderr)
            return 1

        print(f"Checking {env_name}...")
        env_lock = lockdata["envs"][env_name]

        if lockdata["lockfile_version"] >= 2:
            if not env_lock.get("platform"):
                print(
                    f"  platform not checked: env '{env_name}' has no locked platform; run capture"
                )
            else:
                try:
                    if adapter is None:
                        adapter = _pio_adapter_factory()
                    drift = platform_drift(project_dir, env_name, env_lock, adapter)
                    platform_checked = True
                    for problem in drift:
                        problems_found = True
                        print(f"  {label}: {problem}")
                    allow_override = bool(_allow_override_from_env())
                    for override in lock_overrides(project_dir, env_name, adapter):
                        if allow_override:
                            print(f"  WARNING: {override} (allowed by {_ALLOW_OVERRIDE_ENV_VAR})")
                        else:
                            problems_found = True
                            print(
                                f"  {label}: {override}; move {LOCK_INI_NAME} last in "
                                f"extra_configs, or set {_ALLOW_OVERRIDE_ENV_VAR}=1"
                            )
                except ValueError as exc:  # includes ProjectConfigError
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1
                except PioIncompatible as exc:
                    # Not drift: the platform can't be inspected. Libraries are still checked.
                    print(f"  WARNING: platform not checked: {exc}")

        for lib in env_lock["libraries"]:
            if lib["type"] == "local":
                continue

            if not is_installed_at_locked_version(project_dir, env_name, lib):
                problems_found = True
                print(f"  {label}: {_library_drift(lib)}")

    if problems_found and warn:
        print("Lockfile check found differences (reported as warnings; not failing)")
        return 0
    if problems_found:
        print("Lockfile check FAILED — installed state differs from lockfile")
        return 1
    if platform_checked:
        print("Lockfile check passed — libraries, platform and packages match")
    else:
        print("Lockfile check passed — all libraries match")
    return 0


def _library_drift(lib: dict[str, Any]) -> str:
    """How a locked (registry or git) library differs: what the lock expects."""
    if lib["type"] == "git":
        return f"{lib['name']} — expected SHA {lib['sha'][:12]}"
    return f"{lib['name']} — expected {lib['version']}"


def _lib_install_spec(lib: dict[str, Any]) -> Optional[str]:
    """Build a PIO install spec string for a locked library."""
    if lib["type"] == "registry":
        owner = lib.get("owner", "")
        name = lib["name"]
        version = lib["version"]
        return f"{owner}/{name} @ =={version}" if owner else f"{name} @ =={version}"
    elif lib["type"] == "git":
        return f"{lib['url']}#{lib['sha']}"
    return None


# ── PIO internal API (lazy import) ───────────────────────────────────────────


def _import_pio():
    """Lazy-import PlatformIO internals for outdated/update commands."""
    try:
        from platformio.package.manager.library import (  # type: ignore[import-not-found]
            LibraryPackageManager,
        )
        from platformio.package.meta import PackageSpec  # type: ignore[import-not-found]
        from platformio.project.config import ProjectConfig  # type: ignore[import-not-found]

        return LibraryPackageManager, PackageSpec, ProjectConfig
    except ImportError:
        print(
            "Error: PlatformIO Python package is required for outdated/update.\n"
            "Install PlatformIO or run from a PIO environment.",
            file=sys.stderr,
        )
        sys.exit(1)


# Mockable seam — tests replace this to avoid requiring PIO.
_pio_import_fn = _import_pio


# ── PlatformIO adapter (the only door to platform internals) ────────────────
#
# Everything pio-lock needs from PlatformIO's platform/package internals goes through
# PioAdapter. These are not a documented public API, so ``probe()`` checks every import and
# attribute used here and reports the first one that is missing, with the Core version.


@dataclass
class PackageLock:
    """One package an env's platform uses, as installed."""

    name: str
    version: str
    owner: Optional[str] = None
    src_url: Optional[str] = None


@dataclass
class PlatformLock:
    """An env's platform as the user's spec resolves it, with the packages it uses."""

    spec: str
    platform_packages_spec: list[str]
    name: str
    version: str
    src_url: Optional[str]
    packages: list[PackageLock]
    owner: Optional[str] = None


class PioIncompatible(Exception):  # noqa: N818 (name fixed by the 0.4.0 interface)
    """A PlatformIO internal pio-lock relies on is missing or behaves differently."""

    def __init__(self, call: str, core_version: str, cause: str) -> None:
        self.call = call
        self.core_version = core_version
        self.cause = cause
        super().__init__(
            f"PlatformIO Core {core_version} is not compatible with pio-lock: "
            f"{call} failed ({cause})"
        )


class ProjectConfigError(ValueError):
    """platformio.ini (or one of its extra_configs) could not be parsed."""


class PlatformNotInstalled(Exception):  # noqa: N818 (name fixed by the 0.4.0 interface)
    """The platform (or one of its packages) an env resolves to is not installed."""

    def __init__(self, env: str) -> None:
        self.env = env
        super().__init__(
            f"platform for env '{env}' is not installed; run: pio pkg install -e {env}"
        )


# (module, attribute path) pairs PioAdapter uses — checked by probe() in this order.
_PIO_INTERNALS: tuple[tuple[str, str], ...] = (
    ("platformio", "__version__"),
    ("platformio.project.config", "ProjectConfig.read"),
    ("platformio.project.config", "ProjectConfig.get"),
    ("platformio.project.config", "ProjectConfig.items"),
    ("platformio.project.config", "ProjectConfig.BUILTIN_VARS"),
    ("platformio.package.manager.platform", "PlatformPackageManager.get_package"),
    ("platformio.package.manager.tool", "ToolPackageManager.get_installed"),
    ("platformio.platform.factory", "PlatformFactory.new"),
    ("platformio.platform.base", "PlatformBase.configure_project_packages"),
    ("platformio.platform.base", "PlatformBase.packages"),
    ("platformio.platform.base", "PlatformBase.get_package"),
    ("platformio.package.manager.platform", "PlatformPackageManager.fetch_registry_package"),
    ("platformio.package.manager.platform", "PlatformPackageManager.pick_best_registry_version"),
    ("platformio.package.meta", "PackageSpec"),
)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@contextlib.contextmanager
def _chdir(path: Path):
    """Temporarily make ``path`` the working directory (PlatformIO assumes cwd = project)."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PioAdapter:
    """Probed access to PlatformIO's project config and platform internals.

    PlatformIO is imported lazily, inside methods, so this module imports without it.
    Platform resolution only looks at installed packages: it never installs or hits the network.
    """

    def _import(self, module: str) -> Any:
        return importlib.import_module(module)

    def _lookup(self, module: str, attr_path: str) -> Any:
        try:
            obj = self._import(module)
        except Exception as exc:
            raise PioIncompatible(f"import {module}", self.core_version(), _describe(exc)) from exc
        for part in attr_path.split("."):
            try:
                obj = getattr(obj, part)
            except AttributeError as exc:
                raise PioIncompatible(
                    f"{module}.{attr_path}", self.core_version(), "missing attribute"
                ) from exc
        return obj

    @contextlib.contextmanager
    def _calling(self, call: str):
        """Report a PlatformIO internal whose signature or results changed as incompatible."""
        try:
            yield
        except (AttributeError, TypeError) as exc:
            raise PioIncompatible(call, self.core_version(), _describe(exc)) from exc

    def core_version(self) -> str:
        try:
            return str(self._import("platformio").__version__)
        except Exception:
            return "unknown"

    def probe(self) -> None:
        for module, attr_path in _PIO_INTERNALS:
            self._lookup(module, attr_path)

    # ── Project config views ──

    def _config_view(self, project_dir: Path, include_lock_ini: bool) -> tuple[Any, list[Path]]:
        """``_read_config_view``, with config parse failures raised as ProjectConfigError."""
        try:
            return self._read_config_view(project_dir, include_lock_ini)
        except (PioIncompatible, ValueError):
            raise
        except Exception as exc:
            # PlatformIO raises InvalidProjectConfError (not importable without PlatformIO, so
            # matched by name) or configparser errors for an unparsable ini.
            if (
                isinstance(exc, configparser.Error)
                or type(exc).__name__ == "InvalidProjectConfError"
            ):
                raise ProjectConfigError(
                    f"invalid PlatformIO project configuration in "
                    f"{Path(project_dir) / 'platformio.ini'} (or its extra_configs): "
                    f"{_describe(exc)}"
                ) from exc
            raise

    def _read_config_view(
        self, project_dir: Path, include_lock_ini: bool
    ) -> tuple[Any, list[Path]]:
        """Read platformio.ini and its extra_configs like PlatformIO does, but CWD-independent.

        PlatformIO resolves ``extra_configs`` globs against the process CWD and expands
        ``${PROJECT_DIR}``/``${PROJECT_HASH}`` from ``os.getcwd()`` on every ``get()``; here
        both use ``project_dir``. Returns the config and the files in read order.
        """
        project_config = self._lookup("platformio.project.config", "ProjectConfig")
        builtin_vars = self._lookup("platformio.project.config", "ProjectConfig.BUILTIN_VARS")
        project_dir = Path(project_dir).resolve()
        main_ini = project_dir / "platformio.ini"
        lock_ini = os.path.realpath(project_dir / LOCK_INI_NAME)
        with self._calling("platformio.project.config.ProjectConfig"):
            config = project_config(str(main_ini), parse_extra=False)
            # Per-instance override (the class dict is shared): pin the CWD-derived builtins.
            project_hash = hashlib.sha1(str(project_dir).encode("utf-8")).hexdigest()[:10]
            config.BUILTIN_VARS = {
                **builtin_vars,
                "PROJECT_DIR": lambda: str(project_dir),
                "PROJECT_HASH": lambda: f"{project_dir.name}-{project_hash}",
            }
        files = [main_ini]
        seen = {os.path.realpath(main_ini)}

        def read_extra() -> None:
            # Same order as ProjectConfig.read: after each file, walk the (current) patterns.
            with self._calling("platformio.project.config.ProjectConfig.get"):
                patterns = list(config.get("platformio", "extra_configs", []))
            for pattern in patterns:
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = os.path.join(glob.escape(str(project_dir)), pattern)
                for item in glob.glob(pattern, recursive=True):
                    real = os.path.realpath(item)
                    if real in seen or (not include_lock_ini and real == lock_ini):
                        continue
                    seen.add(real)
                    with self._calling("platformio.project.config.ProjectConfig.read"):
                        config.read(item, parse_extra=False)
                    files.append(Path(item))
                    read_extra()

        read_extra()
        return config, files

    def user_config(self, project_dir: Path) -> Any:
        """platformio.ini plus its extra_configs, except pio.lock.ini."""
        return self._config_view(project_dir, include_lock_ini=False)[0]

    def full_config(self, project_dir: Path) -> Any:
        """platformio.ini plus all its extra_configs (what a build sees)."""
        return self._config_view(project_dir, include_lock_ini=True)[0]

    def config_files(self, project_dir: Path) -> list[Path]:
        """The config files PlatformIO reads for this project, in read order."""
        return self._config_view(project_dir, include_lock_ini=True)[1]

    # ── Platform resolution (installed packages only) ──

    @staticmethod
    def _platform_spec(config: Any, env: str) -> str:
        spec = config.get(f"env:{env}", "platform", None)
        if not spec:
            raise ValueError(f"env '{env}' has no platform")
        return str(spec)

    def _installed_platform(self, spec: str, env: str) -> Any:
        manager_cls = self._lookup("platformio.package.manager.platform", "PlatformPackageManager")
        with self._calling("platformio.package.manager.platform.PlatformPackageManager"):
            pkg = manager_cls().get_package(spec)
        if pkg is None:
            raise PlatformNotInstalled(env)
        return pkg

    def resolve_platform(self, project_dir: Path, env: str) -> PlatformLock:
        """Resolve the env's platform and used packages from the user's view (no pio.lock.ini)."""
        project_dir = Path(project_dir).resolve()
        config = self.user_config(project_dir)
        factory = self._lookup("platformio.platform.factory", "PlatformFactory")
        with _chdir(project_dir):
            spec = self._platform_spec(config, env)
            with self._calling("platformio.project.config.ProjectConfig.get"):
                platform_packages = [
                    str(item) for item in config.get(f"env:{env}", "platform_packages", [])
                ]
            pkg = self._installed_platform(spec, env)
            with self._calling("platformio.platform.factory.PlatformFactory.new"):
                platform = factory.new(pkg)
            if not hasattr(platform, "config"):
                raise PioIncompatible(
                    "platformio.platform.base.PlatformBase.config",
                    self.core_version(),
                    "missing attribute",
                )
            # The platform reads ProjectConfig.get_instance() (which includes pio.lock.ini);
            # point it at the user's view before it applies platform_packages.
            platform.config = config
            platform.project_env = env
            with self._calling("platformio.platform.base.PlatformBase.configure_project_packages"):
                platform.configure_project_packages(env)
            packages = self._used_packages(platform, env)
            with self._calling("platformio.package.meta.PackageMetadata"):
                meta = pkg.metadata
                name = str(meta.name)
                version = str(meta.version)
                src_url = str(meta.spec.uri) if meta.spec.external else None
                owner = str(meta.spec.owner) if meta.spec.owner else None
        return PlatformLock(
            spec=spec,
            platform_packages_spec=platform_packages,
            name=name,
            version=version,
            src_url=src_url,
            packages=packages,
            owner=owner,
        )

    def _used_packages(self, platform: Any, env: str) -> list[PackageLock]:
        """The non-optional packages the platform uses (as dump_used_packages), with owners.

        A used package that is not installed means the env is not installed.
        """
        result: list[PackageLock] = []
        with self._calling("platformio.platform.base.PlatformBase.packages"):
            for pkg_name, options in platform.packages.items():
                if options.get("optional"):
                    continue
                pkg = platform.get_package(pkg_name)
                if not pkg or not pkg.metadata:
                    raise PlatformNotInstalled(env)
                meta = pkg.metadata
                result.append(
                    PackageLock(
                        name=str(meta.name),
                        version=str(meta.version),
                        owner=options.get("owner") or meta.spec.owner,
                        src_url=str(meta.spec.uri) if meta.spec.external else None,
                    )
                )
        return result

    def package_installed(
        self, package: dict[str, Any], project_dir: Optional[Path] = None
    ) -> bool:
        """Whether ``package`` (a locked package dict) is installed at exactly its locked version.

        Looks for the exact spec first, then matches by name and exact version among the
        installed tool packages (which also covers packages with no exact spec, e.g. file://).
        """
        manager_cls = self._lookup("platformio.package.manager.tool", "ToolPackageManager")
        name, version = str(package["name"]), str(package["version"])
        call = "platformio.package.manager.tool.ToolPackageManager"
        cwd = _chdir(project_dir) if project_dir else contextlib.nullcontext()
        with cwd, self._calling(call):
            manager = manager_cls()
            if not package.get("src_url"):
                spec = _package_exact_spec(package)
                found = manager.get_package(spec) if spec else None
                if found is not None and str(found.metadata.version) == version:
                    return True
            return any(
                str(pkg.metadata.name) == name and str(pkg.metadata.version) == version
                for pkg in manager.get_installed()
            )

    def latest_platform_version(self, name: str, owner: Optional[str] = None) -> Optional[str]:
        """Newest registry version of a platform, or None if the registry can't be reached."""
        manager_cls = self._lookup("platformio.package.manager.platform", "PlatformPackageManager")
        spec_cls = self._lookup("platformio.package.meta", "PackageSpec")
        call = "platformio.package.manager.platform.PlatformPackageManager.fetch_registry_package"
        try:
            with self._calling(call):
                manager = manager_cls()
                spec = spec_cls(owner=owner, name=name) if owner else spec_cls(name=name)
                package = manager.fetch_registry_package(spec)
                best = manager.pick_best_registry_version(package["versions"])
                return str(best["name"]) if best else None
        except PioIncompatible:
            raise
        except Exception:  # network/registry failure: nothing to report
            return None

    def platform_in_use(self, project_dir: Path, env: str) -> tuple[str, str]:
        """(name, version) of the platform a build of ``env`` uses (pio.lock.ini included)."""
        project_dir = Path(project_dir).resolve()
        config = self.full_config(project_dir)
        with _chdir(project_dir):
            spec = self._platform_spec(config, env)
            pkg = self._installed_platform(spec, env)
            with self._calling("platformio.package.meta.PackageMetadata"):
                return str(pkg.metadata.name), str(pkg.metadata.version)


# Mockable seam — tests replace this with a factory returning a fake adapter.
_pio_adapter_factory: Callable[[], PioAdapter] = PioAdapter


# ── Running under PlatformIO's Python ────────────────────────────────────────
#
# Platform locking needs ``platformio`` importable. When the CLI is installed into some other
# Python (pipx, a project venv), re-run it under PlatformIO's own interpreter.

_REEXEC_ENV = "PIO_LOCK_REEXEC"

_PIO_PYTHON_NOT_FOUND = (
    "pio-lock: PlatformIO's Python not found; platform locking disabled "
    "(install pio-lock into PlatformIO's Python: ~/.platformio/penv/bin/pip install pio-lock)"
)


def _platformio_importable() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("platformio") is not None
    except (ImportError, ValueError):
        return False


def _find_pio_python() -> Optional[str]:
    """PlatformIO's interpreter, from ``pio system info``; None if pio is missing or unparsable."""
    try:
        output = run_cmd(["pio", "system", "info", "--json-output"])
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        value = json.loads(output)["python_exe"]["value"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


def _ensure_pio_python(argv: list[str]) -> None:
    """Re-exec this script under PlatformIO's Python unless ``platformio`` already imports.

    ``PIO_LOCK_REEXEC`` marks the re-exec'd process so it never execs again; once platformio
    imports, the marker is cleared so child processes start clean. If PlatformIO's Python
    can't be found or exec'd, or the marker is already set but platformio still doesn't
    import, warn and carry on: libraries are still locked, platform locking is skipped.

    The re-exec runs in isolated mode (``-I``) so the calling environment's site-packages
    and PYTHON* variables can't shadow PlatformIO's own dependencies.
    """
    if _platformio_importable():
        os.environ.pop(_REEXEC_ENV, None)
        return
    python = None if os.environ.get(_REEXEC_ENV) else _find_pio_python()
    if python:
        env = {**os.environ, _REEXEC_ENV: "1"}
        sys.stdout.flush()
        sys.stderr.flush()
        with contextlib.suppress(OSError):  # unusable interpreter: fall through to the warning
            os.execve(python, [python, "-I", os.path.abspath(__file__), *argv], env)
    print(_PIO_PYTHON_NOT_FOUND, file=sys.stderr)


# ── GitHub-aware update detection ──────────────────────────────────────────


def _parse_github_url(url: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Parse a GitHub URL into (owner/repo, ref).

    Returns (owner, repo, ref) or None if not a GitHub URL.
    ref is None if no #ref is specified.

    Handles:
      https://github.com/owner/repo.git#ref
      https://github.com/owner/repo#ref
      https://github.com/owner/repo
    """
    match = re.match(r"https?://github\.com/([^/]+)/([^/#.]+)(?:\.git)?(?:#(.+))?$", url)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _parse_platform_release_url(url: str) -> Optional[tuple[str, str, str]]:
    """Parse a GitHub release download URL into (owner, repo, version).

    Handles: https://github.com/owner/repo/releases/download/VERSION/filename
    """
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/", url)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_VERSION_TAG_RE = re.compile(r"^v?\d+\.\d+(?:\.\d+)?(?:[._-].+)?$")


def _classify_git_ref(ref: Optional[str]) -> str:
    """Classify a git ref as 'sha', 'tag', 'branch', or 'default'.

    Heuristic:
      - 40 hex chars (or 7+ hex-only) → sha
      - Matches v?DIGIT.DIGIT[.DIGIT] → tag (version-like)
      - None → default (tracks default branch)
      - Everything else → branch
    """
    if ref is None:
        return "default"
    if _SHA_RE.match(ref):
        return "sha"
    if _VERSION_TAG_RE.match(ref):
        return "tag"
    return "branch"


_HIGH_CONFIDENCE_BRANCHES = frozenset(
    {"main", "master", "release", "releases", "stable", "develop"}
)


class GitHubClient:
    """Thin wrapper for GitHub REST API access.

    Tries GITHUB_TOKEN env var first (urllib), then gh CLI fallback.
    """

    def __init__(self, method: str, token: Optional[str] = None):
        self._method = method  # "token" or "gh"
        self._token = token

    def get_json(self, api_path: str) -> Optional[Any]:
        """GET a GitHub API endpoint, return parsed JSON or None on error."""
        if self._method == "token":
            return self._get_via_token(api_path)
        return self._get_via_gh(api_path)

    def _get_via_token(self, api_path: str) -> Optional[Any]:
        import urllib.error
        import urllib.request

        url = f"https://api.github.com/{api_path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "pio-lock",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return None

    def _get_via_gh(self, api_path: str) -> Optional[Any]:
        try:
            result = subprocess.run(
                ["gh", "api", api_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            return None


def _create_github_client() -> Optional[GitHubClient]:
    """Create a GitHubClient using the best available method.

    Priority: GITHUB_TOKEN env var → gh CLI → None.
    """
    import os
    import shutil

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return GitHubClient("token", token)

    if shutil.which("gh"):
        return GitHubClient("gh")

    return None


# Mockable seam for tests.
_create_github_client_fn: Callable[[], Optional[GitHubClient]] = _create_github_client


def _check_git_dep(
    spec_str: str,
    name: str,
    current_version: str,
    github: GitHubClient,
) -> Optional[dict[str, Any]]:
    """Check a single git dep for updates via GitHub API.

    Returns a result dict or None if the dep can't be checked.
    """
    # Extract URL — strip git+ prefix if present
    url = spec_str.split("#")[0].rstrip()
    if url.startswith("git+"):
        url = url[4:]

    parsed = _parse_github_url(url + ("#" + spec_str.split("#")[1] if "#" in spec_str else ""))
    if not parsed:
        return None

    owner, repo, ref = parsed
    ref_type = _classify_git_ref(ref)

    if ref_type == "sha":
        assert ref is not None
        return _check_sha_dep(owner, repo, ref, name, github)
    elif ref_type == "tag":
        assert ref is not None
        return _check_tag_dep(owner, repo, ref, name, github)
    locked_sha = _locked_sha(current_version)
    if locked_sha is None:
        return None
    if ref_type == "branch":
        assert ref is not None
        return _check_branch_dep(owner, repo, ref, locked_sha, name, github)
    # default: the repository's default branch
    repo_info = github.get_json(f"repos/{owner}/{repo}")
    default_branch = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
    if not default_branch:
        return None
    return _check_branch_dep(owner, repo, str(default_branch), locked_sha, name, github)


def _locked_sha(version: str) -> Optional[str]:
    """The resolved commit in a PlatformIO git package version (``+sha.<rev>`` or a bare sha)."""
    match = _GIT_SHA_SUFFIX_RE.search(version)
    if match:
        return match.group(1)
    return version if _SHA_RE.match(version) else None


def _check_branch_dep(
    owner: str, repo: str, branch: str, locked_sha: str, name: str, github: GitHubClient
) -> Optional[dict[str, Any]]:
    """Report a branch whose head is no longer the locked commit; None if unchanged/unreachable."""
    data = github.get_json(f"repos/{owner}/{repo}/branches/{branch}")
    head = data.get("commit", {}).get("sha") if isinstance(data, dict) else None
    if not head or head.startswith(locked_sha) or locked_sha.startswith(head):
        return None
    return {
        "name": name,
        "ref": branch,
        "ref_type": "branch",
        "status": "outdated",
        "current": locked_sha[:7],
        "latest": head[:7],
        "message": f"{branch} moved {locked_sha[:7]} → {head[:7]}",
    }


def _check_sha_dep(
    owner: str, repo: str, sha: str, name: str, github: GitHubClient
) -> Optional[dict[str, Any]]:
    """Check a SHA-pinned dep: find which branch it's on, check for newer commits."""
    short_sha = sha[:7]

    # Find branches where this SHA is the HEAD
    branches = github.get_json(f"repos/{owner}/{repo}/commits/{sha}/branches-where-head")

    if branches and isinstance(branches, list) and len(branches) > 0:
        # SHA is HEAD of at least one branch — it's up to date on those branches
        branch_names = [b["name"] for b in branches]
        best = next(
            (b for b in branch_names if b in _HIGH_CONFIDENCE_BRANCHES),
            branch_names[0],
        )
        confidence = "high" if best in _HIGH_CONFIDENCE_BRANCHES else "medium"
        return {
            "name": name,
            "ref": short_sha,
            "ref_type": "sha",
            "status": "up_to_date",
            "branch": best,
            "confidence": confidence,
            "message": f"up to date on {best}",
        }

    # SHA is not HEAD of any branch — compare against default branch
    compare = github.get_json(f"repos/{owner}/{repo}/compare/{sha}...HEAD")
    if not compare:
        return None

    ahead = compare.get("ahead_by", 0)
    if ahead == 0:
        return {
            "name": name,
            "ref": short_sha,
            "ref_type": "sha",
            "status": "up_to_date",
            "branch": "HEAD",
            "confidence": "high",
            "message": "up to date",
        }

    # Find the default branch name
    default_branch = "HEAD"
    # The compare endpoint doesn't tell us the branch name when using HEAD,
    # but we can get it from the repo info
    repo_info = github.get_json(f"repos/{owner}/{repo}")
    if repo_info:
        default_branch = repo_info.get("default_branch", "HEAD")

    confidence = "high" if default_branch in _HIGH_CONFIDENCE_BRANCHES else "medium"
    new_sha = compare.get("commits", [{}])[-1].get("sha", "")[:7] if compare.get("commits") else ""

    return {
        "name": name,
        "ref": short_sha,
        "ref_type": "sha",
        "status": "outdated",
        "branch": default_branch,
        "confidence": confidence,
        "ahead_by": ahead,
        "latest_sha": new_sha,
        "message": f"{ahead} commit{'s' if ahead != 1 else ''} behind {default_branch}",
    }


_VERSION_NUMERIC_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+_.].*)?$")
_PRERELEASE_SUFFIX_RE = re.compile(
    r"[-_.](?:rc|alpha|beta|dev|pre|nightly)(?:\d|[-._]|$)", re.IGNORECASE
)


def _parse_version_tuple(tag: str) -> Optional[tuple[int, ...]]:
    """Parse the leading numeric version of a tag into a comparable tuple.

    Accepts plain releases (``2.0.0``, ``v1.2``) and tags carrying a pre-release
    or build suffix (``54.03.20-rc1``, ``v2.0.0+build5``). Returns ``None`` if no
    leading numeric version can be extracted.
    """
    match = _VERSION_NUMERIC_RE.match(tag)
    if not match:
        return None
    return tuple(int(x) for x in match.group(1).split("."))


def _is_prerelease_tag(tag: str) -> bool:
    """Return True if a tag carries a pre-release suffix (rc/alpha/beta/...)."""
    return bool(_PRERELEASE_SUFFIX_RE.search(tag))


def _version_sort_key(tag: str) -> tuple[tuple[int, ...], int, str]:
    """Build a sortable key that orders versions correctly across pre-releases.

    Returns ``(numeric_tuple, release_rank, suffix)`` where ``release_rank`` is
    0 for stable releases and -1 for pre-releases (so ``rc1 < stable``), and
    ``suffix`` is the raw post-numeric portion (so ``rc1 < rc2``). Tuples sort
    lexicographically, giving the intended order:

      v2.0.0-rc1 < v2.0.0-rc2 < v2.0.0
    """
    numeric = _parse_version_tuple(tag) or ()
    rank = -1 if _is_prerelease_tag(tag) else 0
    # Strip the leading numeric portion (and any v-prefix) to isolate the suffix
    suffix = re.sub(r"^v?\d+(?:\.\d+)*", "", tag)
    return numeric, rank, suffix


def _check_tag_dep(
    owner: str, repo: str, tag: str, name: str, github: GitHubClient
) -> Optional[dict[str, Any]]:
    """Check a tag-pinned dep: find newer tags with the same naming convention."""
    if _parse_version_tuple(tag) is None:
        return None

    has_v_prefix = tag.startswith("v")
    current_is_prerelease = _is_prerelease_tag(tag)
    current_key = _version_sort_key(tag)

    # Fetch tags (first page, up to 100)
    tags = github.get_json(f"repos/{owner}/{repo}/tags?per_page=100")
    if not tags or not isinstance(tags, list):
        return None

    # Find newer tags with matching format
    newer = []
    for t in tags:
        tag_name = t["name"]
        if _parse_version_tuple(tag_name) is None:
            continue
        # Must match prefix convention
        if has_v_prefix != tag_name.startswith("v"):
            continue
        # Don't promote a stable pin onto a pre-release tag
        if not current_is_prerelease and _is_prerelease_tag(tag_name):
            continue
        key = _version_sort_key(tag_name)
        if key > current_key:
            newer.append((key, tag_name))

    if not newer:
        return {
            "name": name,
            "ref": tag,
            "ref_type": "tag",
            "status": "up_to_date",
            "message": "latest tag",
        }

    newer.sort(reverse=True)
    latest_tag = newer[0][1]

    return {
        "name": name,
        "ref": tag,
        "ref_type": "tag",
        "status": "outdated",
        "latest_tag": latest_tag,
        "message": f"{tag} → {latest_tag} available",
    }


def _check_git_deps(entries: list[dict[str, Any]], github: GitHubClient) -> list[dict[str, Any]]:
    """Check all git deps for updates via GitHub API."""
    results = []
    seen = set()
    for entry in entries:
        if entry["type"] != "git":
            continue
        name = entry["name"]
        if name in seen:
            continue
        seen.add(name)

        result = _check_git_dep(entry["spec_str"], name, entry["current"], github)
        if result:
            results.append(result)
    return results


def _check_platform(
    project_dir: Path, env_name: str, github: GitHubClient
) -> Optional[dict[str, Any]]:
    """Check if a newer platform release is available on GitHub.

    Reports separately:
      - ``outdated``: a newer release within the same major exists
      - ``major_update_available``: only a higher-major release is newer
      - ``up_to_date``: current is the highest matching tag
    """
    platform_url = get_platform_url(project_dir, env_name)
    if platform_url == "unknown":
        return None

    parsed = _parse_platform_release_url(platform_url)
    if not parsed:
        return None

    owner, repo, current_version = parsed
    current_ver = _parse_version_tuple(current_version)
    if current_ver is None:
        return None
    current_major = current_ver[0]

    releases = github.get_json(f"repos/{owner}/{repo}/releases?per_page=50")
    if not releases or not isinstance(releases, list):
        return None

    current_is_prerelease = _is_prerelease_tag(current_version)
    current_key = _version_sort_key(current_version)
    same_major: list[tuple[Any, str]] = []
    higher_major: list[tuple[Any, str]] = []

    for rel in releases:
        tag = rel.get("tag_name", "")
        if rel.get("draft") or rel.get("prerelease"):
            continue
        ver = _parse_version_tuple(tag)
        if ver is None:
            continue
        if not current_is_prerelease and _is_prerelease_tag(tag):
            continue
        key = _version_sort_key(tag)
        if key <= current_key:
            continue
        if ver[0] == current_major:
            same_major.append((key, tag))
        else:
            higher_major.append((key, tag))

    if same_major:
        same_major.sort(reverse=True)
        latest_tag = same_major[0][1]
        extra = ""
        if higher_major:
            higher_major.sort(reverse=True)
            extra = f" (cross-major {higher_major[0][1]} also available)"
        return {
            "name": repo,
            "current": current_version,
            "status": "outdated",
            "latest": latest_tag,
            "latest_major": higher_major[0][1] if higher_major else None,
            "message": f"{current_version} → {latest_tag} available{extra}",
        }

    if higher_major:
        higher_major.sort(reverse=True)
        latest_major_tag = higher_major[0][1]
        return {
            "name": repo,
            "current": current_version,
            "status": "major_update_available",
            "latest_major": latest_major_tag,
            "message": (
                f"{current_version} → {latest_major_tag} (cross-major update; "
                "manual review recommended)"
            ),
        }

    return {
        "name": repo,
        "current": current_version,
        "status": "up_to_date",
        "message": "latest release",
    }


def _check_registry_platform(
    project_dir: Path, env: str, env_lock: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Compare a registry platform's locked version with the newest registry version.

    None for URL/git platforms (see ``_check_platform``), unlocked envs, or an unreachable
    registry. Raises PioIncompatible if PlatformIO's registry API changed.
    """
    platform = env_lock.get("platform")
    if not platform or platform.get("src_url"):
        return None
    name, current = str(platform["name"]), str(platform["version"])
    latest = _pio_adapter_factory().latest_platform_version(name, platform.get("owner"))
    if not latest:
        return None
    base = {"env": env, "name": name, "current": current}
    if _version_sort_key(latest) > _version_sort_key(current):
        return {
            **base,
            "status": "outdated",
            "latest": latest,
            "message": f"{current} → {latest} available",
        }
    return {**base, "status": "up_to_date", "message": "latest release"}


# ── Outdated / update helpers ────────────────────────────────────────────────


def _get_outdated_entries(
    project_dir: Path,
    envs: list[str],
    lib_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Check all deps across envs for available updates using PIO's API."""
    lib_pkg_mgr_cls, pkg_spec_cls, proj_config_cls = _pio_import_fn()

    config = proj_config_cls.get_instance(str(project_dir / "platformio.ini"))
    entries: list[dict[str, Any]] = []

    for env_name in envs:
        lib_deps = config.get(f"env:{env_name}", "lib_deps", [])
        libdeps_dir = config.get(f"env:{env_name}", "libdeps_dir", None)
        if libdeps_dir:
            pkg_dir = Path(libdeps_dir) / env_name
        else:
            pkg_dir = project_dir / ".pio" / "libdeps" / env_name

        pm = lib_pkg_mgr_cls(str(pkg_dir))

        for dep_str in lib_deps:
            spec = pkg_spec_cls(dep_str)

            # Skip local/symlinked deps
            if spec.symlink:
                continue

            if lib_filter and lib_filter.lower() not in spec.name.lower():
                continue

            pkg = pm.get_package(spec)
            if pkg is None:
                continue

            result = pm.outdated(pkg, spec)
            name = pkg.metadata.name

            dep_type = "git" if spec.external else "registry"

            entry: dict[str, Any] = {
                "env": env_name,
                "name": name,
                "current": str(result.current),
                "wanted": str(result.wanted) if result.wanted else None,
                "latest": str(result.latest) if result.latest else None,
                "type": dep_type,
                "spec_str": dep_str,
                "is_outdated": result.is_outdated(allow_incompatible=True),
                "detached": result.detached,
            }
            entries.append(entry)

    return entries


def _registry_platform_results(project_dir: Path, envs: list[str]) -> list[dict[str, Any]]:
    """Registry-platform checks for the locked envs; skipped when there is no lockfile."""
    lockfile_path = project_dir / LOCKFILE_NAME
    if not lockfile_path.exists():
        return []
    locked_envs = load_lockfile(lockfile_path).get("envs", {})
    results: list[dict[str, Any]] = []
    for env_name in envs:
        env_lock = locked_envs.get(env_name)
        if not env_lock:
            continue
        try:
            result = _check_registry_platform(project_dir, env_name, env_lock)
        except PioIncompatible as exc:
            print(f"  Warning: registry platform check skipped: {exc}", file=sys.stderr)
            return results
        if result:
            results.append(result)
    return results


def outdated(
    project_dir: Path,
    envs: list[str],
    output_json: bool = False,
) -> int:
    """Check for outdated dependencies. Returns 0 if up-to-date, 1 if updates available."""
    if not (project_dir / "platformio.ini").exists():
        print(f"Error: no platformio.ini in {project_dir}", file=sys.stderr)
        return 1

    entries = _get_outdated_entries(project_dir, envs)

    # GitHub-aware checks for git deps and platform
    github = _create_github_client_fn()
    git_results: list[dict[str, Any]] = []
    platform_result: Optional[dict[str, Any]] = None
    github_skipped_reason: Optional[str] = None

    if github:
        git_results = _check_git_deps(entries, github)
        platform_result = _check_platform(project_dir, envs[0], github)
    else:
        github_skipped_reason = (
            "no GITHUB_TOKEN set and gh CLI not found. "
            "Set GITHUB_TOKEN or install gh for git dependency update detection."
        )

    registry_platform_results = _registry_platform_results(project_dir, envs)

    has_outdated = any(e["is_outdated"] for e in entries)
    has_registry_platform_outdated = any(
        r["status"] == "outdated" for r in registry_platform_results
    )
    has_git_outdated = any(r["status"] == "outdated" for r in git_results)
    has_platform_outdated = platform_result is not None and platform_result["status"] in (
        "outdated",
        "major_update_available",
    )

    if output_json:
        data: dict[str, Any] = {
            "libraries": entries,
            "git_deps": git_results,
            "platform": platform_result,
            "registry_platforms": registry_platform_results,
        }
        if github_skipped_reason:
            data["github_skipped"] = github_skipped_reason
        print(json.dumps(data, indent=2))
        return (
            1
            if (
                has_outdated
                or has_git_outdated
                or has_platform_outdated
                or has_registry_platform_outdated
            )
            else 0
        )

    # Table output — registry and PIO-managed deps
    registry_entries = [e for e in entries if e["type"] == "registry"]
    if registry_entries:
        header = f"{'Library':<40} {'Current':<12} {'Wanted':<12} {'Latest':<12}"
        print(header)
        print("-" * len(header))

        for entry in registry_entries:
            if entry["detached"]:
                print(f"  {entry['name']:<38} (pinned — skipped)")
                continue

            marker = " *" if entry["is_outdated"] else "  "
            current = entry["current"]
            wanted = entry["wanted"] or "-"
            latest = entry["latest"] or "-"
            print(f"{marker}{entry['name']:<38} {current:<12} {wanted:<12} {latest:<12}")

    # Git dependencies section
    git_entries_from_pio = [e for e in entries if e["type"] == "git"]
    if git_entries_from_pio:
        print(f"\n{'Git dependencies'}")
        print("-" * 76)

        # Build a lookup from name → GitHub check result
        gh_by_name = {r["name"]: r for r in git_results}

        for entry in git_entries_from_pio:
            name = entry["name"]
            gh = gh_by_name.get(name)

            if gh:
                marker = " *" if gh["status"] == "outdated" else "  "
                ref = gh["ref"]
                msg = gh["message"]
                confidence = gh.get("confidence", "")
                conf_str = f" [{confidence}]" if confidence else ""
                print(f"{marker}{name:<38} {ref:<12} {msg}{conf_str}")
            else:
                # No GitHub result — show PIO info
                current = entry["current"][:20]
                print(f"  {name:<38} {current}")

    # Platform section
    if platform_result:
        print(f"\n{'Platform'}")
        print("-" * 76)
        status = platform_result["status"]
        if status == "outdated":
            marker = " *"
        elif status == "major_update_available":
            marker = "!!"
        else:
            marker = "  "
        print(f"{marker}{platform_result['name']:<38} {platform_result['message']}")

    if registry_platform_results:
        print(f"\n{'Registry platforms'}")
        print("-" * 76)
        for reg in registry_platform_results:
            marker = " *" if reg["status"] == "outdated" else "  "
            print(f"{marker}{reg['env'] + ': ' + reg['name']:<38} {reg['message']}")

    if github_skipped_reason:
        print(f"\nNote: GitHub checks skipped — {github_skipped_reason}")

    if has_outdated or has_git_outdated or has_platform_outdated or has_registry_platform_outdated:
        print("\n* = update available")
        return 1
    else:
        print("\nAll dependencies are up to date.")
        return 0


class ConfigSourceTracker:
    """Track which config file each option value comes from.

    PlatformIO's ProjectConfig merges all extra_configs into a flat
    configparser dict, discarding source file information. This class
    independently parses each file to build a source map.

    Works with any config object that has a ``_parsed`` attribute
    (list of file paths that were read, in order).
    """

    def __init__(self, config: Any, fallback_path: Optional[Path] = None):
        self._files: list[str] = getattr(config, "_parsed", [])
        if not self._files and fallback_path:
            self._files = [str(fallback_path)]
        # (section, option) -> filepath — last writer wins (matches configparser)
        self._source_map: dict[tuple[str, str], str] = {}
        self._build_source_map()

    def _build_source_map(self) -> None:
        for filepath in self._files:
            parser = configparser.ConfigParser()
            try:
                parser.read(filepath, encoding="utf-8")
            except configparser.Error:
                continue
            for section in parser.sections():
                for option in parser.options(section):
                    self._source_map[(section, option)] = filepath

    def get_source(self, section: str, option: str) -> Optional[Path]:
        """Return the file that defines this (section, option) pair."""
        filepath = self._source_map.get((section, option))
        return Path(filepath) if filepath else None

    def find_file_for_value(self, value: str) -> Optional[Path]:
        """Search config files for a string (e.g. a lib_deps spec).

        For multi-value options like lib_deps, individual entries may be
        on separate lines within a section. This does a text search across
        all config files, checking the last file first (since it wins on
        conflicts).
        """
        # Search in reverse order — last file wins for configparser
        for filepath in reversed(self._files):
            path = Path(filepath)
            if not path.is_file():
                continue
            if value in path.read_text():
                return path
        return None

    @property
    def files(self) -> list[Path]:
        """All config files that were parsed, in load order."""
        return [Path(f) for f in self._files]


def _build_updated_spec(spec_str: str, new_version: str) -> Optional[str]:
    """Build an updated spec string preserving the range prefix.

    E.g. "acme/Foo @ ^1.0.0" + "1.2.0" -> "acme/Foo @ ^1.2.0"
    Returns None if the spec can't be parsed.
    """
    if " @ " not in spec_str:
        return None

    prefix, version_part = spec_str.rsplit(" @ ", 1)

    # Extract range prefix (^, ~, >=, etc.)
    match = re.match(r"^([~^>=<]+)?(.+)$", version_part)
    if not match:
        return None

    range_prefix = match.group(1) or ""
    return f"{prefix} @ {range_prefix}{new_version}"


def update(
    project_dir: Path,
    envs: list[str],
    dry_run: bool = True,
    lib_filter: Optional[str] = None,
) -> int:
    """Update config files with newer dependency versions.

    Searches platformio.ini and all extra_configs files to find where
    each lib_deps entry is declared, and updates it in the correct file.
    """
    if not (project_dir / "platformio.ini").exists():
        print(f"Error: no platformio.ini in {project_dir}", file=sys.stderr)
        return 1

    _pm_cls, _spec_cls, proj_config_cls = _pio_import_fn()
    config = proj_config_cls.get_instance(str(project_dir / "platformio.ini"))
    tracker = ConfigSourceTracker(config, fallback_path=project_dir / "platformio.ini")

    entries = _get_outdated_entries(project_dir, envs, lib_filter=lib_filter)
    outdated_entries = [e for e in entries if e["is_outdated"]]

    if not outdated_entries:
        print("All dependencies are up to date.")
        return 0

    # Build replacements, locating each spec in the correct config file
    # replacements: {file_path: [(old_spec, new_spec), ...]}
    replacements: dict[Path, list[tuple[str, str]]] = {}
    for entry in outdated_entries:
        wanted = entry["wanted"] or entry["latest"]
        if not wanted:
            continue
        new_spec = _build_updated_spec(entry["spec_str"], wanted)
        if not new_spec:
            continue

        source_file = tracker.find_file_for_value(entry["spec_str"])
        if source_file is None:
            print(
                f"  Warning: could not locate {entry['spec_str']!r} in any config file",
                file=sys.stderr,
            )
            continue

        print(f"  {entry['name']}: {entry['current']} -> {wanted}")
        print(f"    {entry['spec_str']}  =>  {new_spec}")
        print(f"    in {source_file}")

        if source_file not in replacements:
            replacements[source_file] = []
        replacements[source_file].append((entry["spec_str"], new_spec))

    if not replacements:
        print("No updatable specs found in config files.")
        return 0

    if dry_run:
        print("\nDry run — no files modified.")
        return 0

    # Apply replacements to each config file. Restrict substitutions to
    # `lib_deps` blocks so identical strings appearing elsewhere (comments,
    # build_flags, etc.) are not accidentally rewritten.
    lib_deps_re = re.compile(
        r"(^[ \t]*lib_deps[ \t]*=)([^\n]*(?:\n[ \t]+[^\n]*)*)",
        re.MULTILINE,
    )
    for filepath, specs in replacements.items():
        content = filepath.read_text()

        # Create backup alongside the original
        backup_path = filepath.with_suffix(filepath.suffix + ".bak")
        backup_path.write_text(content)
        print(f"\nBackup: {backup_path}")

        def _rewrite_block(match: re.Match[str], specs=specs) -> str:
            head, body = match.group(1), match.group(2)
            for old_spec, new_spec in specs:
                body = body.replace(old_spec, new_spec)
            return head + body

        new_content = lib_deps_re.sub(_rewrite_block, content)

        filepath.write_text(new_content)
        print(f"Updated: {filepath}")

    print("\nRun `pio pkg install` then `pio-lock capture` to lock the new versions.")
    return 0


# ── PlatformIO extra_script integration ──────────────────────────────────────


_LOAD_MARKER = "PIO_LOCK_LOADED_BY"


def _load_origin(env: Any) -> str:
    """Return how this copy of pio_lock was loaded.

    ``platform``: imported as module ``pio_lock`` (e.g. by pioarduino).
    ``library``: run as a library.json ``extraScript`` (lib_deps), which PlatformIO
    executes with a library builder environment rather than the project environment.
    ``extra_scripts``: a ``pre:``/``post:`` line in platformio.ini.
    """
    if __name__ == "pio_lock":
        return "platform"
    try:
        if env.get("PIOBUILDER") is not None or env.get("_PIO_LIB_BUILDER"):
            return "library"
    except Exception:
        pass
    try:
        libdeps = env.subst("$PROJECT_LIBDEPS_DIR")
        here = str(Path(__file__).resolve())
        if libdeps and here.startswith(str(Path(libdeps).resolve()) + os.sep):
            # Under libdeps and not a project-level script line.
            return "library"
    except Exception:
        pass
    return "extra_scripts"


def _already_loaded(env: Any) -> bool:
    """Record the first load on the shared DefaultEnvironment; report repeats.

    A module global would not do: the SConscript exec and a pioarduino import are
    different module objects, but all share the SCons DefaultEnvironment.
    """
    try:
        from SCons.Script import DefaultEnvironment  # type: ignore[import-not-found]

        shared = DefaultEnvironment()
    except ImportError:
        shared = env
    origin = _load_origin(env)
    first = shared.get(_LOAD_MARKER)
    if first is None:
        shared[_LOAD_MARKER] = origin
        return False
    if first == "platform":
        print(
            "pio-lock: already loaded by the platform (custom_pio_lock); "
            "remove pio-lock from lib_deps or extra_scripts"
        )
    else:
        print(
            "pio-lock: already loaded via lib_deps; the extra_scripts line for "
            "pio_lock.py is deprecated since 0.4.0 and can be removed"
        )
    return True


_DRIFT_INI_OPTION = "custom_pio_lock_drift"
_ALLOW_OVERRIDE_INI_OPTION = "custom_pio_lock_allow_override"
_DRIFT_PROMPT = "Continue with this build? [c]ontinue / [a]bort:"
_INCLUDE_HINT = (
    f"pio-lock: add 'extra_configs = {LOCK_INI_NAME}' (or run pio-lock init) to pin the platform"
)


def _project_option(env: Any, name: str) -> str:
    """A platformio.ini option for the env being built, lower-cased ("" when unavailable)."""
    try:
        return str(env.GetProjectOption(name, "") or "").strip().lower()
    except Exception:
        return ""


def drift_policy(env: Any) -> str:
    """``"fail"`` or ``"warn"``: ``PIO_LOCK_DRIFT``, then ``custom_pio_lock_drift``; default fail.

    The policy only matters in release mode: outside it, drift always warns.
    """
    policy = _drift_policy_from_env()
    if policy:
        return policy
    value = _project_option(env, _DRIFT_INI_OPTION)
    return value if value in ("warn", "fail") else "fail"


def _allow_override(env: Any) -> bool:
    """``PIO_LOCK_ALLOW_OVERRIDE``, then ``custom_pio_lock_allow_override``; default False."""
    from_env = _allow_override_from_env()
    if from_env is not None:
        return from_env
    return _project_option(env, _ALLOW_OVERRIDE_INI_OPTION) in ("true", "1", "yes")


def _is_interactive() -> bool:
    """Whether someone can answer a prompt from inside the build.

    PlatformIO runs SCons with stdout piped (never a tty there) and passes ``ISATTY=1`` when
    its own stdout is a terminal; stdin is inherited.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    try:
        from SCons.Script import ARGUMENTS  # type: ignore[import-not-found]

        return bool(int(ARGUMENTS.get("ISATTY", 0)))
    except (ImportError, TypeError, ValueError):
        return False


class _BuildFindings:
    """What build_checks found for one env, and the commands that fix it."""

    def __init__(self, env_name: str) -> None:
        self.problems: list[str] = []  # drift: fails a release build under the fail policy
        self.warnings: list[str] = []  # reported, never fails
        self.hints: list[str] = []
        self.fixes: list[str] = []
        self.restore_fix = (
            f"run: pio-lock restore -e {env_name}  "
            "(installs the locked versions; then re-run the build)"
        )
        self.capture_fix = (
            f"run: pio-lock capture -e {env_name}  (records the current config in the lock)"
        )

    def problem(self, text: str, fix: Optional[str] = None) -> None:
        self.problems.append(text)
        if fix and fix not in self.fixes:
            self.fixes.append(fix)


def _platform_build_findings(
    project_dir: Path,
    env_name: str,
    env_lock: dict[str, Any],
    adapter: PioAdapter,
    allow_override: bool,
    findings: _BuildFindings,
) -> None:
    """Platform checks for a v2 lock: include hint, drift, later-config overrides."""
    if _lock_ini_position(project_dir, adapter.config_files(project_dir)) is None:
        findings.hints.append(_INCLUDE_HINT)
        return
    if not env_lock.get("platform"):
        findings.warnings.append(
            f"platform not checked: env '{env_name}' has no locked platform; "
            f"run: pio-lock capture -e {env_name}"
        )
        return
    for problem in platform_drift(project_dir, env_name, env_lock, adapter):
        stale = problem.endswith("run capture")  # _lock_staleness: the ini changed
        findings.problem(problem, findings.capture_fix if stale else findings.restore_fix)
    for override in lock_overrides(project_dir, env_name, adapter):
        if allow_override:
            findings.warnings.append(f"{override} (allowed)")
        else:
            findings.problem(
                f"{override}; move {LOCK_INI_NAME} last in extra_configs, or set "
                f"{_ALLOW_OVERRIDE_ENV_VAR}=1 / {_ALLOW_OVERRIDE_INI_OPTION} = true to allow it"
            )


def _collect_build_findings(
    project_dir: Path, env_name: str, env: Any, release: bool
) -> _BuildFindings:
    findings = _BuildFindings(env_name)
    try:
        lockdata = load_lockfile(Path(project_dir) / LOCKFILE_NAME)
    except (OSError, ValueError) as exc:
        findings.problem(f"can't read {LOCKFILE_NAME}: {exc}", findings.capture_fix)
        return findings
    env_lock = lockdata.get("envs", {}).get(env_name)
    if env_lock is None:
        findings.hints.append(
            f"env '{env_name}' is not in {LOCKFILE_NAME}; run pio-lock capture -e {env_name}"
        )
        return findings

    for lib in env_lock.get("libraries", []):
        if lib.get("type") != "local" and not is_installed_at_locked_version(
            project_dir, env_name, lib
        ):
            findings.problem(_library_drift(lib), findings.restore_fix)

    adapter = _pio_adapter_factory()
    if lockdata["lockfile_version"] < 2:
        findings.hints.append(
            f"platform not checked: lockfile is v1; run: pio-lock capture -e {env_name} "
            "to lock the platform"
        )
    else:
        try:
            _platform_build_findings(
                project_dir, env_name, env_lock, adapter, _allow_override(env), findings
            )
        except (PioIncompatible, ValueError) as exc:
            # A release build must not silently lose its platform lock.
            message = f"platform not checked: {exc}"
            (findings.problems if release else findings.warnings).append(message)

    running = adapter.core_version()
    locked = str(lockdata.get("pio_core_version") or "unknown")
    if "unknown" not in (running, locked) and running != locked:
        findings.warnings.append(
            f"PlatformIO Core {running} differs from the locked {locked} (warning only)"
        )
    return findings


def _ask_to_continue() -> bool:
    """Prompt until continue or abort; an empty answer or EOF aborts.

    The prompt is a whole line: PlatformIO forwards the build's output line by line, so a
    prompt without a newline would never show.
    """
    while True:
        print(_DRIFT_PROMPT, flush=True)
        line = sys.stdin.readline()
        answer = line.strip().lower()
        if answer in ("c", "continue"):
            return True
        if not answer or answer in ("a", "abort"):
            return False


def build_checks(project_dir: Path, env_name: str, env: Any) -> None:
    """Compare the env being built with pio.lock.json; warn, prompt, or stop the build.

    Runs from the build hook on every normal build (never for pio-lock's own targets).
    Drift fails a release build unless the drift policy is ``warn``; outside release mode it
    warns, and in an interactive terminal asks whether to continue. The build can't switch
    platforms or libraries itself, so the guidance is to install (restore) and re-run.
    """
    if _is_lock_target_run(_requested_targets(env)):
        return
    if not (Path(project_dir) / LOCKFILE_NAME).exists():
        return
    release = _is_release_mode(load_snapshot(project_dir), env)
    found = _collect_build_findings(project_dir, env_name, env, release)

    fail = bool(found.problems) and release and drift_policy(env) == "fail"
    if found.problems or found.warnings:
        label = "ERROR" if fail else "WARNING"
        print(f"pio-lock: env '{env_name}' compared with {LOCKFILE_NAME}:")
        for problem in found.problems:
            print(f"  {label}: {problem}")
        for warning in found.warnings:
            print(f"  WARNING: {warning}")
        for fix in found.fixes:
            print(f"  {fix}")
    for hint in found.hints:
        print(hint if hint.startswith("pio-lock:") else f"pio-lock: {hint}")

    if not found.problems:
        return
    if fail:
        sys.stdout.flush()  # keep the details above the error when both go to one pipe
        print(
            f"pio-lock: ERROR: build stopped: env '{env_name}' differs from {LOCKFILE_NAME} "
            f"in release mode (to build anyway: {_DRIFT_ENV_VAR}=warn or "
            f"{_DRIFT_INI_OPTION} = warn)",
            file=sys.stderr,
        )
        env.Exit(1)
    elif not release and _is_interactive():
        print("  This build can't switch versions itself: install them (see above) and re-run.")
        if not _ask_to_continue():
            env.Exit(1)


def register_pio_targets(env: Any) -> None:
    """Register custom PIO targets when loaded via lib_deps or as an extra_script.

    Preferred (0.4.0+): add pio-lock to ``lib_deps``. Legacy:
        extra_scripts = pre:path/to/pio_lock.py
    """
    if _already_loaded(env):
        return
    project_dir = Path(env.subst("$PROJECT_DIR"))

    # Collect environments from the PIO config
    try:
        config = env.GetProjectConfig()
        envs = config.envs()
    except Exception:
        envs = [env.subst("$PIOENV")]

    def _make_action(fn: Callable[..., int]) -> Callable[..., None]:
        def action(*_args: Any, **_kwargs: Any) -> None:
            rc = fn()
            if rc != 0:
                env.Exit(rc)

        return action

    targets = [
        (
            "lock-capture",
            lambda: capture(project_dir, envs),
            "Lock Capture",
            "Capture dependency versions into pio.lock.json",
        ),
        (
            "lock-check",
            lambda: check(project_dir, envs),
            "Lock Check",
            "Verify installed deps match pio.lock.json",
        ),
        (
            "lock-restore",
            lambda: restore(project_dir, envs),
            "Lock Restore",
            "Install exact versions from pio.lock.json",
        ),
        (
            "snapshot-capture",
            lambda: snapshot_capture(project_dir),
            "Snapshot Capture",
            "Generate build_snapshot.json from current HEAD",
        ),
        (
            "snapshot-check",
            lambda: snapshot_check(project_dir),
            "Snapshot Check",
            "Verify build_snapshot.json matches HEAD",
        ),
        (
            "snapshot-clear",
            lambda: snapshot_clear(project_dir),
            "Snapshot Clear",
            "Delete build_snapshot.json",
        ),
        (
            "snapshot-print",
            lambda: snapshot_print(project_dir),
            "Snapshot Print",
            "Display build_snapshot.json contents",
        ),
    ]

    for name, fn, title, description in targets:
        env.AddCustomTarget(
            name,
            None,
            _make_action(fn),
            title=title,
            description=description,
            always_build=True,
        )

    # Auto-manage snapshot on every build
    checks = not _is_lock_target_run(_requested_targets(env))
    snapshot_auto_manage(project_dir, env, checks=checks)

    # Compare the env being built with the lock (skipped for pio-lock's own targets)
    build_checks(project_dir, env.subst("$PIOENV"), env)


# Auto-register targets when loaded as a PIO extra_script
try:
    Import("env")  # type: ignore[name-defined]
    register_pio_targets(env)  # type: ignore[name-defined]
except NameError:
    # Not running inside PIO/SCons — this is normal for CLI usage
    pass


# ── init ─────────────────────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^\s*\[([^\]]*)\]")
_EXTRA_CONFIGS_RE = re.compile(r"^extra_configs\s*[=:](.*)$", re.IGNORECASE)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith((";", "#"))


def _lock_ini_listed(entries: list[str]) -> bool:
    for entry in entries:
        for item in entry.split(","):
            if item.strip() in (LOCK_INI_NAME, f"./{LOCK_INI_NAME}"):
                return True
    return False


def init(project_dir: Path, dry_run: bool = False) -> int:
    """Add ``pio.lock.ini`` to ``[platformio] extra_configs`` in platformio.ini.

    A plain text edit (no configparser round trip) so comments and layout survive.
    """
    ini_path = Path(project_dir) / "platformio.ini"
    if not ini_path.is_file():
        print(f"Error: {ini_path} not found", file=sys.stderr)
        return 1
    with open(ini_path, newline="") as f:
        original = f.read()
    nl = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl

    section_start = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m or line[0] in " \t":
            continue
        if section_start is None:
            if m.group(1).strip() == "platformio":
                section_start = i
        elif section_end == len(lines):
            section_end = i
            break

    if section_start is None:
        new_line = f"extra_configs = {LOCK_INI_NAME}{nl}"
        lines[0:0] = [f"[platformio]{nl}", new_line, nl]
        changed = new_line
    else:
        opt_idx = None
        for i in range(section_start + 1, section_end):
            if lines[i][:1] not in (" ", "\t") and _EXTRA_CONFIGS_RE.match(
                lines[i].rstrip("\r\n")
            ):
                opt_idx = i
                break
        if opt_idx is None:
            new_line = f"extra_configs = {LOCK_INI_NAME}{nl}"
            lines.insert(section_start + 1, new_line)
            changed = new_line
        else:
            first = _EXTRA_CONFIGS_RE.match(lines[opt_idx].rstrip("\r\n"))
            assert first is not None
            entries = [first.group(1)]
            last_entry_idx = opt_idx
            indent = "    "
            j = opt_idx + 1
            while j < section_end and (lines[j][:1] in (" ", "\t") or not lines[j].strip()):
                text = lines[j].strip()
                if text and not _is_comment(lines[j]):
                    entries.append(text)
                    last_entry_idx = j
                    indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                j += 1
            if _lock_ini_listed(entries):
                print(f"{LOCK_INI_NAME} already included in {ini_path}")
                return 0
            new_line = f"{indent}{LOCK_INI_NAME}{nl}"
            lines.insert(last_entry_idx + 1, new_line)
            changed = new_line

    verb = "Would add" if dry_run else "Added"
    print(f"{verb} {LOCK_INI_NAME} to extra_configs in {ini_path}:")
    print(f"  {changed.rstrip()}")
    if not dry_run:
        with open(ini_path, "w", newline="") as f:
            f.write("".join(lines))
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pio-lock",
        description="PlatformIO dependency lockfile tool for reproducible builds",
    )
    parser.add_argument(
        "-d",
        "--project-dir",
        default=".",
        help="PlatformIO project directory (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser(
        "capture",
        help="Capture resolved dependency versions into pio.lock.json",
    )
    p_capture.add_argument(
        "-e",
        "--env",
        action="append",
        required=True,
        help="PIO environment to capture (repeatable)",
    )
    p_capture.add_argument(
        "--output",
        default=None,
        help="Output path (default: <project>/pio.lock.json)",
    )

    p_restore = sub.add_parser(
        "restore",
        help="Install exact versions from pio.lock.json",
    )
    p_restore.add_argument(
        "-e",
        "--env",
        action="append",
        required=True,
        help="PIO environment to restore (repeatable)",
    )

    p_check = sub.add_parser(
        "check",
        help="Verify the installed platform, packages and libraries match pio.lock.json",
    )
    p_check.add_argument(
        "-e",
        "--env",
        action="append",
        required=True,
        help="PIO environment to check (repeatable)",
    )
    p_check.add_argument(
        "--warn-only",
        action="store_true",
        help="Report drift as warnings and exit 0 (same as PIO_LOCK_DRIFT=warn)",
    )

    p_outdated = sub.add_parser(
        "outdated",
        help="Check for outdated dependencies",
    )
    p_outdated.add_argument(
        "-e",
        "--env",
        action="append",
        required=True,
        help="PIO environment to check (repeatable)",
    )
    p_outdated.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output as JSON",
    )

    p_update = sub.add_parser(
        "update",
        help="Update platformio.ini with newer dependency versions",
    )
    p_update.add_argument(
        "-e",
        "--env",
        action="append",
        required=True,
        help="PIO environment to update (repeatable)",
    )
    p_update.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what would change without modifying files (default)",
    )
    p_update.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to platformio.ini",
    )
    p_update.add_argument(
        "--lib",
        dest="lib_filter",
        help="Update only a specific library (name substring match)",
    )

    p_init = sub.add_parser(
        "init",
        help=f"Add {LOCK_INI_NAME} to [platformio] extra_configs in platformio.ini",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the change without modifying platformio.ini",
    )

    # Snapshot subcommands (no -e required — they operate on the project)
    snapshot_cmds = {
        "snapshot-capture": "Generate build_snapshot.json from current HEAD",
        "snapshot-check": "Verify build_snapshot.json matches HEAD",
        "snapshot-clear": "Delete build_snapshot.json",
        "snapshot-print": "Display build_snapshot.json contents",
    }
    for name, help_text in snapshot_cmds.items():
        sub.add_parser(name, help=help_text)

    args = parser.parse_args(argv)
    if argv is None and args.command != "init":
        # A real CLI run (sys.argv). In-process callers pass argv and are never replaced.
        # init is a pure text edit and works from any Python.
        _ensure_pio_python(sys.argv[1:])
    project_dir = Path(args.project_dir).resolve()

    if args.command == "init":
        return init(project_dir, dry_run=args.dry_run)

    if args.command == "capture":
        output = Path(args.output) if args.output else None
        return capture(project_dir, args.env, output)
    elif args.command == "restore":
        return restore(project_dir, args.env)
    elif args.command == "check":
        return check(project_dir, args.env, warn_only=args.warn_only)
    elif args.command == "outdated":
        return outdated(project_dir, args.env, output_json=args.output_json)
    elif args.command == "update":
        dry = not args.apply
        return update(project_dir, args.env, dry_run=dry, lib_filter=args.lib_filter)

    # Snapshot commands — dispatch by suffix
    snapshot_dispatch = {
        "snapshot-capture": snapshot_capture,
        "snapshot-check": snapshot_check,
        "snapshot-clear": snapshot_clear,
        "snapshot-print": snapshot_print,
    }
    if args.command in snapshot_dispatch:
        return snapshot_dispatch[args.command](project_dir)

    return 1


if __name__ == "__main__":
    sys.exit(main())
