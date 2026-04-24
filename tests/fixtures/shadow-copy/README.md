# shadow-copy

Tests that PlatformIO shadow copy directories (e.g. `NimBLE-Arduino@src-abc123`)
are correctly skipped during capture. Only the real library directory should appear
in the lockfile.
