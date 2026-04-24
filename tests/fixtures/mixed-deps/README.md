# mixed-deps

Tests capture of a project with both a registry library (semver range) and a
local library (`file://`). Verifies that local libs are recorded as type "local"
with their path, while registry libs get exact versions.
