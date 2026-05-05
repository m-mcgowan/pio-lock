# Changelog

All notable changes to this project will be documented in this file.
Follows [Keep a Changelog](https://keepachangelog.com/) conventions.

## [Unreleased]

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

[Unreleased]: https://github.com/m-mcgowan/pio-lock/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/m-mcgowan/pio-lock/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/m-mcgowan/pio-lock/releases/tag/v0.1.0
