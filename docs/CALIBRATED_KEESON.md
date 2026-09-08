# Calibrated Keeson/Ergomotion position control (fork)

This opt-in fork is based on upstream v3.7.1. It ports a local, user-tested
position fix into source modules with a per-frame configuration profile.
The fork itself still requires hardware acceptance testing after installation.
Do not infer compatibility from an Ergomotion/Askona brand name alone.

## Configuration

Use the existing Adjustable Bed config entry, **Configure**:

- Bed type: **Keeson**; protocol variant: **Ergomotion**.
- Motor count: **2**; **Disable angle sensing**: off.
- **Calibrated EDFE16 positions (two motors)**: on.
- Enter both **measured raw sensor maxima** for this physical frame.
- **Allow brief feedback probes on explicit position commands**: opt in only
  if brief movement is acceptable when a command starts without fresh feedback.

There are no default calibration maxima. One tested frame measured back=17700,
legs=11586, but these values are not claimed to apply to other frames.
The percentage is a normalized sensor reading, not a verified linear angle.
Calibration does not use an address, config-entry ID or advertised-name match.
Without opt-in, the upstream controller and entity behavior remain in use.

## Behavior

The existing back and legs covers retain their unique IDs. They advertise
OPEN, CLOSE, STOP and SET_POSITION (15). Open seeks 100%; Close seeks 0%.
Numbers and the set-position service use the same coordinator seek path.
Only controller-declared `feedback_seek_axes` use the new lifecycle; the
existing general seek implementation remains unchanged for other controllers.

The controller accepts only a 16-byte `ED FE 16` frame with an additive checksum
of 255 modulo 256. Bytes 3:5 and 5:7 are little-endian back/legs values.
A raw value of 65535 or more than 110% of its configured maximum is rejected.
Values at or below 0.5% map to zero; upper values are clamped to 100%.
Unknown fields are not decoded as light or massage state.

New subscriptions clear old feedback. Each callback is bound to a subscription
and BLE client, including re-subscription on the same client. After disconnect,
covers report an unknown position instead of inventing zero. A cached
measurement is not accepted as fresh feedback for the next seek.

Only an explicit position command may acquire feedback through motion. Startup,
connection and background reads never probe. The command first waits passively
for 0.25 seconds, then permits at most two opposite-direction probes if enabled.
Each nominal 100 ms probe is followed by the existing Ergomotion release/STOP.
This is software timing after the write completes, not a hardware-timed pulse.
After STOP, feedback is awaited passively for up to 1.2 seconds after the first
probe and 3.0 seconds after the second. The final wait accommodates a recorded
cold start whose first valid report arrived 2.056 seconds after the second STOP.
No additional movement is sent while waiting; cancellation, session checks and
the 1-second feedback freshness requirement still apply.

Seeking requires feedback no older than 1 second, stops within 0.5 percentage
points or at measured target crossing, and never publishes a fabricated target.
It aborts on wrong direction (>1 percentage point), no progress (2.5 seconds),
link/session change or timeout (60 seconds). GATT writes are bounded to
2 seconds and a timed-out motor write is not retried. STOP cleanup stays inside
the command lock and is shielded against repeated task cancellation. Loss of
BLE may prevent delivery of STOP; the integration cannot guarantee physical
stopping without a functioning link.

## Evidence and limits

The decoder was replayed against two private recordings: 580 and 1780 frames,
with 19 and 56 rejected respectively. The reason for the 75 invalid snapshots
is unresolved. Public tests use synthetic frames; private recordings, device
identities and local installer files are not shipped.

This work uses the supplied local implementation and measured capture evidence.
It is not a clean-room APK analysis and does not claim to satisfy the upstream
Phase 4 APK-analysis contribution gate. A future upstream PR must address that
maintainer workflow separately; no upstream PR is opened by this release.

Hardware acceptance: for both axes test idle reconnect (>40s), 10→25→0%,
25→50%, Stop during motion and immediately after sending a target, a newer
target replacing an older target, physical-remote feedback, and cross-axis
independence. Confirm the initial movement ends and no delayed probe starts
after Stop. Keep the physical remote available during acceptance.

## HACS

Fork: https://github.com/VladLikov/ha-adjustable-bed

The `askona-position` default branch contains the patch. `main` stays inherited
from upstream. The fork release/tag and manifest identify the custom version.
HACS offers the default branch and published releases; there is no reliance on
an arbitrary non-default branch being selectable in the download dialog.

When switching repository sources with the same `adjustable_bed` domain, first
back up the complete HA configuration and original component tree. Uninstall
only the downloaded upstream repository in HACS, then download the fork before
the next restart. Do not delete the Adjustable Bed entry under Devices & services.
Do not leave both HACS sources marked installed for the same component path.
Local v1/v2 users must restore v2 first and v1 second before replacing the source.
Never run either local patch installer on the fork.
