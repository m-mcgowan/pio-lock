# Changelog

All notable changes to this project will be documented in this file.
Follows [Keep a Changelog](https://keepachangelog.com/) conventions.

## [Unreleased]

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
