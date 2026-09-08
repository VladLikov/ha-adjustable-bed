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

A calibrated position request expires if command-lock waiting, connection and
initialization take more than 10 seconds. The age is checked before preparation,
after preparation and immediately before handing the command to the controller.
An expired request never starts acquisition probes or target movement. A new
explicit request is needed once the connection is ready. This is a request-age
policy, not a shorter BLE transport timeout: an in-flight connection and its
cleanup may still take longer, and no transport task is forcefully abandoned.
The age limit does not interrupt a seek that started within the limit; its
existing motion timeout and STOP cleanup remain in effect.

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
Target feedback interrupts the interval between motor writes. If the target is
reported while a GATT write is pending, STOP follows completion of that bounded
write without an additional interval. Intermediate feedback does not accelerate
command writes. Transport delay and mechanical stopping distance still limit
position accuracy; the requested percentage is never substituted for feedback.
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


## Experimental native targets for confirmed Ergomotion 633 (askona.6)

Enable **Experimental native positions (confirmed Ergomotion 633 only; no motion
probes)** in the same options form. It is disabled by default and requires the
calibrated-position toggle, two motors, Ergomotion variant and angle sensing.
Keep the existing raw maxima for rollback. In native mode these maxima and the
motion-probe option do not control position seeking. The preceding directional
seek description applies only with this new toggle off.

The Askona Android 5.4.2 CustomPreset slider path supplies a single absolute
head/feet pair. This mode sends that pair once through the existing serialized
GATT writer. It does not pulse direction commands or send STOP on successful
completion. Both axes must have fresh feedback from this subscription: the
unchanged coordinate is taken from the same snapshot, never filled with zero.
Initial feedback is awaited passively for up to 5 seconds; absence fails before
sending any movement. This may still fail on a cold connection that sends no
unsolicited notification. No motion-based fallback is attempted.

Native percentages use the notification high-byte coordinates and model-633
limits head=68, feet=44. A 50% request maps to 34/22; resolution is about 1.47/2.27
percentage points. Targets are rounded to the nearest coordinate, while state
always comes from actual notifications. Low raw-byte fractions are not native
app coordinates. This intentionally replaces raw-calibration scaling in this
mode. It does not change HomeKit inversion or Yandex configuration.

The write has a 2-second timeout with no retry on uncertain delivery. During
observation the existing 1-second freshness requirement applies, with 5 seconds
without coordinate progress and a 60-second operation watchdog. These are local
watchdogs, not timings extracted from the app. Target completion requires a
post-dispatch notification at or beyond the quantized target. Lost connection,
cancellation, timeout or reverse motion triggers best-effort directional STOP
on the pinned connection. Physical cancellation of autonomous native targets by
that STOP is **unverified**; the native app slider path did not establish it.
Do not assume a successful STOP write proves mechanical stopping.

Evidence: the 633 slider, mapper and state paths in the complete Android 5.4.2
artifact were checked against DEX smali. This is partial, non-clean-room personal
fork analysis, not proof that the iOS implementation is identical and not
hardware acceptance. It must not be auto-selected by brand, name or BLE address.
The user confirmed model 633; Element and 180x200 are outside this opt-in mode.
First hardware checks must cover Stop, preservation of the other section, a
modest target in each direction, real percentage reporting and idle reconnect.


### askona.7 endpoint decoding correction

Native 633 coordinates are signed bytes before clamping to 0..68/44, matching
the native app. In particular, the recorded FFFF endpoint is read as -1 and
clamped to zero, rather than rejected by the legacy raw-16-bit decoder.
Frame length, header and checksum validation remain required. The calibrated
directional mode retains its existing sentinel checks. A recorded-frame
regression verifies position recovery and subsequent native target dispatch.
