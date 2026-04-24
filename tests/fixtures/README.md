# Test Fixtures

Each subdirectory is a minimal PlatformIO project that reproduces a specific
scenario. Integration tests automatically discover and test all fixture projects.

## Creating a fixture for a bug report

1. Create a new directory: `tests/fixtures/<issue-name>/`
2. Add a minimal `platformio.ini` with only the config needed to reproduce
3. Create `.pio/libdeps/<env>/` with the library state (use `.piopm` files
   for registry libs and `.git` markers for git libs)
4. Optionally add `expected.json` with expected library entries per env
5. Add a `README.md` describing the scenario

## Structure

```
tests/fixtures/<issue-name>/
  platformio.ini          # minimal PIO config
  README.md               # what this tests / reproduces
  .pio/
    libdeps/
      <env>/
        <library>/
          .piopm          # for registry libs
          .git            # marker for git libs
          library.json    # optional metadata
  expected.json           # optional expected output (for validation)
```

## expected.json format

```json
{
  "env_name": [
    {"name": "LibName", "type": "registry", "version": "1.2.3"},
    {"name": "GitLib", "type": "git", "sha": "abc123..."}
  ]
}
```

Only the fields you specify are checked — you don't need to list every field.
