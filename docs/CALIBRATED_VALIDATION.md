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

## Delayed initial feedback fix: 3.7.1+askona.3

2026-09-08, CPython 3.14.7, Home Assistant 2026.9.1.

- Full integration suite: 2839 passed, 2 skipped in 283.26 seconds.
- Focused calibrated controller suite: 34 passed.
- Regression with the recorded 2.056-second initial feedback delay failed on
  the previous code with the original two-probe ConnectionError, and passed
  with the correction, reaching the requested 50% using simulated feedback.
- Cancellation, client replacement, re-subscription and stale feedback during
  the final passive wait tested; no extra motor writes during acquisition.
- Ruff and Pyright on changed Python files: passed, zero type errors/warnings.
- Manifest and project versions both 3.7.1+askona.3.
- Archive contains 116 tracked component files; file hashes verified.

No commands were sent to the physical bed. This version has not been installed
on the user's HA by Codex, and hardware acceptance is pending. The change is
based on the owner's supplied log and explicit permission to proceed without
APK analysis; it does not claim upstream Phase 4 compliance.

## Responsive target STOP: 3.7.1+askona.4

2026-09-08, CPython 3.14.7, Home Assistant 2026.9.1.

- Full integration suite: 2851 passed, 2 skipped in 285.18 seconds.
- Calibrated controller suite: 46 passed.
- Target-arrival regression failed on askona.3 before the patch and passed after.
- Both axes and directions covered, with target feedback arriving during a GATT
  write or during the interval. STOP never overlaps a pending movement write.
- Intermediate feedback preserves write pacing; cancellation, client replacement
  and stale feedback are observed before the next movement write.
- Ruff and Pyright on changed Python files: passed, zero errors/warnings.
- Manifest and pyproject versions both 3.7.1+askona.4.
- Deployment component file hashes checked against the archive.

No hardware commands or changes to the user's HA were made. The fixed software
pause is removed; transport and mechanical delays still limit final accuracy.
Hardware verification of this release remains pending. Initial probes and the
3-second final acquisition wait are unchanged.
