# Source-port validation

Date: 2026-09-08. Baseline: upstream `v3.7.1`, commit
`2491a740a3db5128f6695daa2de0b679261d4400`.
The baseline Git blobs for `beds/keeson.py`, `coordinator.py` and `cover.py`
match the original hashes in the supplied local v2 installer.

Local environment: macOS, CPython 3.14.7, Home Assistant 2026.9.1,
pytest-homeassistant-custom-component 0.13.364.

- Full suite, `pytest -n 4 -q`: **2834 passed, 2 skipped**, 284.28 seconds.
- Ruff 0.15.16, `check custom_components tests`: passed.
- Pyright 1.1.410, `custom_components tests`: **0 errors, 0 warnings**.
- Mypy for the new controller and profile modules: passed.
- Calibration options and English translation JSON: validated.
- Manifest and project versions: both `3.7.1+askona.1`.
- Private recording replay: 2360 notifications, 2285 accepted, 75 rejected.
  No private recordings or local identity bindings are included in the repository.

The added tests cover decoder validation, calibration rejection, two-probe limit,
probe opt-in, passive reads, old callback isolation even on the same BLE client,
cold starts on both axes, Stop/cancellation cleanup, stale feedback, wrong
movement direction, no progress, feedback loss, write/seek timeouts, connection
loss, real coordinator preemption during connect, command-lock cleanup ordering,
native cover behavior, subscriptions, and options flow persistence/validation.

Not yet verified: installation on the user's Synology/HA 2026.8.3, physical motor
behavior of this source port, Alice speech parsing and HomeKit UI on the actual
installation. Existing local v2 user feedback is evidence for the predecessor,
not hardware acceptance of this release.
