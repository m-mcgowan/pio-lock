# pio-lock

Dependency lockfile for [PlatformIO](https://platformio.org/) — reproducible builds for embedded projects.

PlatformIO resolves library versions at install time but has no lockfile to record what was actually installed. If your `platformio.ini` uses semver ranges (`^1.8.3`), git branches, or unpinned git URLs, a fresh install can silently pick up different versions. `pio-lock` fixes this.

## Status

| Feature | Design | Docs | Impl | Tests | Examples | Since | Updated |
|---------|--------|------|------|-------|----------|-------|---------|
| **Dependency scanning** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_scan](tests/test_scan.py) | [fixtures/](tests/fixtures/) | | |
| **Lockfile generation** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_commands](tests/test_commands.py) | [minimal-native](tests/fixtures/minimal-native/) | | |
| **Build snapshot** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_snapshot](tests/test_snapshot.py) | | | |
| **Outdated detection** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_outdated](tests/test_outdated.py) | | | |
| **Update commands** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_commands](tests/test_commands.py) | | | |
| **PlatformIO integration** | [pio_lock.py](pio_lock.py) | [README](README.md) | [pio_lock.py](pio_lock.py) | [test_integration](tests/test_integration.py) | | | |
| **GitHub Actions integration** | | [README](README.md) | | | | | |
| **Config source tracking** | [pio_lock.py](pio_lock.py) | | [pio_lock.py](pio_lock.py) | [test_commands](tests/test_commands.py) | | | |

## How it works

`pio-lock` scans your installed PlatformIO dependencies and records their exact versions in a `pio.lock.json` file that you commit alongside `platformio.ini`. Later, `restore` reinstalls those exact versions, and `check` verifies nothing has drifted.

It captures:
- **Registry libraries** — exact resolved version from `.piopm` metadata
- **Git libraries** — exact commit SHA from the installed repo
- **Local libraries** — recorded for completeness, skipped during restore
- **Global packages** — framework, toolchain, and tool versions from `~/.platformio/packages/`
- **Platform URL** — the resolved platform specification

## Quick start

```bash
# 1. Install your dependencies normally
pio pkg install -e my_env

# 2. Capture the resolved state
python pio_lock.py capture -e my_env

# 3. Commit the lockfile
git add pio.lock.json

# 4. On another machine (or in CI), restore exact versions
python pio_lock.py restore -e my_env

# 5. Build as usual
pio run -e my_env
```

## Usage

### Standalone CLI

```bash
python pio_lock.py capture -e esp32s3-idf
python pio_lock.py capture -e esp32s3-idf -e esp32s3   # multiple envs
python pio_lock.py capture -e esp32s3-idf --output artifacts/pio.lock.json
python pio_lock.py restore -e esp32s3-idf
python pio_lock.py check -e esp32s3-idf   # exit 0 = match, 1 = drift
```

| Flag | Description |
|------|-------------|
| `-d, --project-dir` | PlatformIO project directory (default: `.`) |
| `-e, --env` | Environment name (repeatable) |
| `--output` | Output path for capture (default: `pio.lock.json` in project dir) |

### PlatformIO custom targets

Add `pio-lock` as a library dependency and reference its script:

```ini
[env:myenv]
lib_deps =
    https://github.com/m-mcgowan/pio-lock.git

extra_scripts =
    pre:${PROJECT_LIBDEPS_DIR}/${PIOENV}/pio-lock/pio_lock.py
```

PlatformIO downloads pio-lock into `.pio/libdeps/<env>/` and the
`extra_scripts` path references it there.

Alternatively, if you prefer not to use `lib_deps`, copy `pio_lock.py` into
your project and reference it directly:

```ini
[env:myenv]
extra_scripts = pre:scripts/pio_lock.py
```

This registers custom targets:

```bash
pio run -t lock-capture    # capture dependency versions
pio run -t lock-check      # verify no drift
pio run -t lock-restore    # restore from lockfile
```

## GitHub Actions

When `pio-lock` is configured via `lib_deps` and `extra_scripts` in your
`platformio.ini`, the custom targets are available directly:

```yaml
- name: Restore locked dependencies
  run: pio run -t lock-restore -e esp32s3-idf

- name: Verify lockfile
  run: pio run -t lock-check -e esp32s3-idf
```

Or integrate into an existing cache strategy:

```yaml
- name: Cache PlatformIO Dependencies
  id: cache
  uses: actions/cache@v4
  with:
    path: |
      ~/.platformio/platforms
      ~/.platformio/packages
      .pio/libdeps
    key: pio-${{ hashFiles('pio.lock.json', 'platformio.ini') }}
    restore-keys: pio-

- name: Restore locked dependencies
  if: steps.cache.outputs.cache-hit != 'true'
  run: pio run -t lock-restore -e esp32s3-idf

- name: Verify lockfile consistency
  run: pio run -t lock-check -e esp32s3-idf
```

## Lockfile format

```json
{
  "_comment": "Generated by pio-lock. Do not edit manually.",
  "generated_at": "2026-02-19T10:00:00Z",
  "generated_from_commit": "abc1234",
  "pio_core_version": "6.1.18",
  "platform_url": "https://github.com/.../platform-espressif32.zip",
  "global_packages": {
    "framework-arduinoespressif32": "3.3.6",
    "toolchain-xtensa-esp-elf": "14.2.0+20251107"
  },
  "envs": {
    "esp32s3-idf": {
      "libraries": [
        {
          "name": "Blues Wireless Notecard",
          "type": "registry",
          "version": "1.8.4",
          "owner": "blues"
        },
        {
          "name": "esp-nimble-cpp",
          "type": "git",
          "url": "https://github.com/user/esp-nimble-cpp.git",
          "sha": "32e7ca2d2d387d74eaeac09751049778faea4040"
        },
        {
          "name": "my-local-lib",
          "type": "local",
          "path": "file://lib/my-local-lib"
        }
      ]
    }
  }
}
```

## What this solves

| Problem | How pio-lock helps |
|---------|-------------------|
| `lib @ ^1.8.3` resolves to 1.8.4 today, 1.8.5 tomorrow | Locks to exact version (`==1.8.4`) |
| Git dep on a branch (`#fix/something`) | Locks to exact commit SHA |
| Unpinned git URL (no `#ref` at all) | Locks to exact commit SHA |
| Transitive deps resolved at install time | All resolved deps captured in lockfile |
| "What libraries were in release v1.2.3?" | Lockfile committed with the release tag |
| CI cache hit with stale deps | `check` command detects drift |

## What this doesn't solve

- ~~**Bit-for-bit identical binaries** — ESP-IDF embeds timestamps; same source doesn't guarantee identical bytes~~ The build snapshot feature now sets `SOURCE_DATE_EPOCH` to produce deterministic `__DATE__`/`__TIME__` macros, enabling reproducible builds
- **Global toolchain pinning** — Recorded for auditing; the platform URL already pins these in practice
- **PlatformIO Core version enforcement** — Recorded in lockfile for reference

## Contributing

### Developer setup

```bash
./scripts/dev-setup.sh
source .venv/bin/activate
```

This creates a venv, installs dev dependencies, and sets up git hooks:
- **pre-commit** — ruff lint/format + unit tests
- **pre-push** — unit tests on regular pushes, full suite (unit + integration) on tag pushes

### Running tests

```bash
# Unit tests only (fast, no PIO needed)
pytest -m "not integration"

# All tests including integration (needs PIO installed)
pytest

# Integration tests only
pytest -m integration
```

### Reproducing a bug

To add a test case from a bug report:

1. Create `tests/fixtures/<issue-name>/`
2. Add a minimal `platformio.ini`
3. Add `.pio/libdeps/<env>/` with the library state that triggers the bug
4. Optionally add `expected.json` with expected output
5. Add a `README.md` describing the scenario

See `tests/fixtures/README.md` for the full format.

### Code quality

```bash
ruff check .          # lint
ruff format --check . # format
mypy pio_lock.py      # type check
```

Pre-commit hooks run lint + unit tests automatically. Integration tests run on tags.

## Requirements

- Python 3.8+
- PlatformIO Core CLI (`pio`)
- git CLI

## License

MIT
