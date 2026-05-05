# messy-ini-multi-env

Realistic multi-env project where `lib_deps` is shared via `extra_configs`,
plus a comment and `build_flags` that contain spec-like strings.

Verifies that:

- Capture iterates all envs and resolves their per-env libdeps independently.
- Spec strings appearing inside comments or other options (`build_flags`,
  `[env]` defaults) are correctly ignored — they are not dependencies and
  must not be picked up.
- `[env]` defaults propagate to per-env sections through PIO's normal
  config inheritance.
