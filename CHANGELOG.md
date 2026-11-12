# Changelog

All notable changes to this project will be documented in this file.
Follows [Keep a Changelog](https://keepachangelog.com/) conventions.

## [Unreleased]

## [0.4.0] - 2026-11-11

pio-lock now locks each env's platform and platform packages as well as its libraries, can be
set up with a `lib_deps` line alone, and records the git ref you asked for. See the README's
"Pinning the platform", "What a plain `pio run` pins" and "Updating" sections.

### Breaking
- **Lockfile format v2.** `pio.lock.json` gains `lockfile_version: 2` and a per-env `platform`
  object (spec, resolved name/version/source and the packages the env uses). The top-level
  `global_packages` and `platform_url` keys are removed. v1 files are still read (libraries work
  as before; platform checks are skipped with a hint). **Fix:** re-run
  `pio-lock capture -e <env>` (or `pio run -t lock-capture`) and commit `pio.lock.json` and the
  new `pio.lock.ini`.
- **`update` applies by default.** It installs the newer versions, re-captures the lock, and
  rewrites versions in `platformio.ini` that move within your declared range. Use `--dry-run` to
  preview or `--lock-only` to leave `platformio.ini` alone. `--apply` is accepted as a deprecated
  no-op. No `.bak` files are written any more.
- **Drift fails release builds.** When pio-lock is loaded in a build, it compares the env with
  `pio.lock.json` on every build. In release mode (`PIO_RELEASE_BUILD=1`,
  `custom_reproducible_release = true`, or a pinned snapshot) library or platform drift stops
  the build. **Fix** (to keep 0.3.x behaviour): `PIO_LOCK_DRIFT=warn` or
  `custom_pio_lock_drift = warn`.
- A config file read after `pio.lock.ini` that sets `platform` or `platform_packages` for a
  locked env is an error (drift). **Fix:** move `pio.lock.ini` last in `extra_configs`, or allow
  it with `PIO_LOCK_ALLOW_OVERRIDE=1` / `custom_pio_lock_allow_override = true`.

### Added
- Platform locking: `capture` records each env's platform (registry `<owner>/<name>@<version>`,
  git/URL platforms pinned to the resolved commit) and the packages it uses, and writes
  `pio.lock.ini`. Include it with `[platformio] extra_configs = pio.lock.ini` and a plain
  `pio run` uses the locked platform and packages. Your own `platform_packages` are kept at their
  exact versions; packages installed from local `file://` paths are recorded and checked but
  left out of `pio.lock.ini`.
- `pio-lock init` adds `pio.lock.ini` to `extra_configs` (`--dry-run` previews).
- `capture --install` installs an env's platform (and the packages it needs) from
  `platformio.ini` when it isn't installed, then captures it. With `pio.lock.ini` included this
  is how a changed `platform =` gets into the lock: `pio pkg install -e <env>` installs the
  locked platform instead. `update` installs the platforms it moves to the same way, once per
  env (envs sharing a platform can need different packages), and fails if a moved env's
  platform still doesn't resolve rather than keep its old lock. A failure inside PlatformIO's
  install is reported as `installing platform for env '<env>' failed`, exit 1.
- `check` and `restore` cover the platform and its packages; `restore` installs them before the
  libraries. `check --warn-only` reports drift as warnings and exits 0.
- Build-time drift check: library drift, platform drift, a stale lock (`platform =` or
  `platform_packages` changed since capture) and a PlatformIO Core version mismatch (warning
  only). Outside release mode drift warns; in a terminal the build asks whether to continue
  (also in IDE task terminals — `PIO_LOCK_DRIFT=warn` skips the prompt). An env missing from the
  lock prints a hint; with a v1 lock or without the `pio.lock.ini` include, libraries are still
  checked and the platform is skipped with a hint (`pio-lock check` does the same). `clean`,
  `cleanall` and `fullclean` skip the checks.
- `lib_deps` alone sets pio-lock up: `library.json` loads `pio_lock.py` as the library's
  extra script. pio-lock is loaded once per build, however many entry points name it.
- `pip install pio-lock` gives a `pio-lock` command. When `platformio` isn't importable from
  that Python, it re-runs itself under PlatformIO's Python (found with `pio system info`); if
  that fails it locks libraries only and says how to install into PlatformIO's Python.
- Git ref tracking: git libraries record the requested `ref` and `ref_type`. `outdated` reports
  branches whose head moved past the locked commit, newer tags, newer registry platforms and
  newer release URLs.
- `update` moves the lock: bare specs and branch refs move only the lock; versions within
  range also rewrite the ini in the style written (`^1.8.3` → `^1.8.5`); exact pins move within
  the same major; `--beyond-range` crosses the declared range; specs it can't rewrite are
  reported for a hand edit; edits to a section shared with other envs name those envs. Old git
  checkouts are removed after a move. With `--lock-only`, a library is never moved outside what
  its `lib_deps` spec allows (past the range, off an exact pin, or to another git tag) — a plain
  `pio run` would install a second, matching copy — and the move is reported instead. An env
  whose `platform =` changed since capture records the platform it now resolves to (or keeps its
  old lock with a `capture --install` hint), never the new spec at the old version. This follows
  pnpm / Yarn 2+ rather than Cargo / npm.
- `capture -e <env>` updates only that env's entries and prunes envs no longer in
  `platformio.ini`; pio-lock itself is left out of the library list. If PlatformIO's internals
  are incompatible, the previous platform lock is kept with a warning.
- Duplicate library copies are reported. Installed duplicates (several copies of one library in
  `.pio/libdeps/<env>`, matched by manifest name or git URL) warn in `capture`, `restore`,
  `check` and the build, with who requested each copy and which `library.json` pins a
  dependency exactly. Linked duplicates (two copies built into one image) are checked just
  before linking and fail a locked project's release build under the drift policy (without
  `pio.lock.json` they warn). See the README's "Duplicate
  library copies".
- Each locked library records its libdeps directory as `dir`.
- `-d/--project-dir` is accepted after the subcommand as well as before it.
- The `lock-*` targets now act on the env being built (`pio run -e <env> -t lock-capture`), so
  another env's missing platform can't fail them. Without `-e`, PlatformIO runs them for each
  env in `default_envs` (every env when it is unset) that loads pio-lock; to cover every env,
  pass each `-e` or use the CLI (`pio-lock capture -e a -e b`). (0.3.x acted on every env each
  time.)
- Every command checks PlatformIO's internals once at the start, so an unsupported Core is
  reported once, by the first missing piece.
- Integration tests build real projects on the `native` platform; CI tests PlatformIO 6.1.18,
  6.2.x and latest, plus a weekly run. `make test-pio PIO=<version>` runs them locally against
  any PlatformIO version in isolated directories.

### Fixed
- In release mode with no `build_snapshot.json`, `pio run -t snapshot-capture` (the fix the
  error message suggests) failed with the same error.
- Requesting a `lock-*` or `snapshot-*` target no longer runs the build-time checks or rewrites
  `build_snapshot.json` first, which hid the staleness `snapshot-check` exists to report.
- An unparsable `platformio.ini` gives an error message instead of a traceback.
- `capture` no longer crashes when the `pio` CLI isn't on `PATH`.
- An unreadable `pio.lock.json` (for example a merge conflict) no longer crashes: `capture`
  replaces it with a warning; `check`, `restore`, `outdated` and `update` exit 1 with
  `pio.lock.json is unreadable`.
- `outdated` and `update` read `platformio.ini` independently of the working directory and take
  `libdeps_dir` from `[platformio]`.
- `check` compared every lock entry of a library installed twice against the first directory
  found, giving a permanent `DRIFT: … expected <version>` (or a SHA that depended on directory
  order); duplicated libraries now get the duplicate warning instead, and entries are compared
  against their recorded `dir` when it exists.
- `capture` skipped every `<name>@src-<hash>` directory, so a git library installed only there
  was missing from the lock; these checkouts are now recorded.

### Deprecated
- The `lib_deps` + `extra_scripts = pre:${PROJECT_LIBDEPS_DIR}/${PIOENV}/pio-lock/pio_lock.py`
  combination. The `lib_deps` line now loads pio-lock by itself; the `pre:` line keeps working
  but prints a note that it can be removed. Copy-into-project `pre:` lines are unaffected.

### For integrators
- pioarduino and other platforms that `import pio_lock`: `register_pio_targets(env)` is
  unchanged. A second load in the same build (for example `custom_pio_lock` plus a `lib_deps`
  line) is skipped with a message.
- The lockfile format changed (see Breaking). The Python API and lockfile format aren't
  guaranteed stable before 1.0 — pin the pio-lock version you integrate with.

## [0.3.0] - 2026-11-10

### Changed
- The build snapshot takes `SOURCE_DATE_EPOCH` from the HEAD commit's committer time on a clean
  tree, so two machines building the same commit stamp the same time. A dirty tree (or no git)
  falls back to the current time and prints `non-reproducible: ...`.
- Untracked files no longer make the tree dirty.
- Snapshots record `epoch_source` (`commit` or `time`). A snapshot whose source no longer fits the
  tree (including one written by 0.2.x) is regenerated.
- The snapshot's epoch overrides any `SOURCE_DATE_EPOCH` already set in the environment.
- `snapshot-check` applies the same staleness rule as the build, so a 0.2.x snapshot reports STALE.
  In release mode a snapshot stale only by its epoch source says so, rather than showing two
  identical commits.

## [0.2.0] - 2026-05-05

### Fixed
- `restore` no longer passes `--skip-dependencies` to `pio pkg install`. On a
  clean environment, transitive dependencies of locked libraries now install
  correctly — `lock-restore` can bootstrap a fresh checkout.
- `update` no longer rewrites spec strings that appear outside `lib_deps`
  blocks. Identical strings in comments, `build_flags`, or other options
  are left untouched. Substitutions are now scoped via a regex that matches
  `lib_deps = …` blocks only.
- Snapshot and lockfile writes are now atomic (tempfile + `os.fsync` +
  `os.replace`, with cleanup on failure). An interrupted write can no
  longer leave a half-written JSON file.
- `_parse_version_tuple` accepts pre-release and build suffixes
  (`54.03.20-rc1`, `v2.0.0+build5`); previously these returned `None`,
  silently hiding the tag from update detection.
- `_check_platform` now reports cross-major releases as a distinct
  `major_update_available` status with a `!!` marker instead of silently
  ignoring them. `outdated` returns exit code 1 when a cross-major update
  exists.
- Pre-release ordering: `_check_tag_dep` and `_check_platform` now compare
  versions via a sort key that ranks `rc1 < rc2 < stable`, so a prerelease
  pin can advance to a newer prerelease, and a stable pin is never
  promoted onto a pre-release tag.

### Added
- New integration fixture `messy-ini-multi-env` exercising multi-env
  `lib_deps` shared via `extra_configs`, with comments and `build_flags`
  containing spec-like strings.
- CI now enforces an 80% coverage floor (`--cov-fail-under=80`).
- Argv-level pinning for `restore` tests catches accidental flag drift at
  PR review time.
- Real-world tag fixtures parametrize version-parsing tests over 15 tag
  shapes from production projects.

## [0.1.0] - 2026-04-23

### Added
- Build snapshot for reproducible builds with SOURCE_DATE_EPOCH support
- GitHub-aware update detection for git dependencies and platform URLs
- ConfigSourceTracker for config file origin tracking across extra_configs
- Outdated and update commands with CI report output and coverage
- Dependency scanning: registry (semver ranges and exact), git deps, transitive deps
- Lockfile generation, restore, and drift check commands
- PlatformIO custom target integration (lock-capture, lock-check, lock-restore)
- GitHub Actions integration examples with caching
- Test fixtures: mixed-deps, minimal-native, registry-semver, registry-exact, shadow-copy

### Fixed
- Update command now searches extra_configs files for lib_deps

[Unreleased]: https://github.com/m-mcgowan/pio-lock/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/m-mcgowan/pio-lock/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/m-mcgowan/pio-lock/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/m-mcgowan/pio-lock/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/m-mcgowan/pio-lock/releases/tag/v0.1.0
