"""Opt-in 633 absolute targets from the Askona Android 5.4.2 slider path.

One packet carries both coordinates. Actual feedback is required to preserve
its other axis. This is experimental: physical cancellation of an autonomous
native target by the inherited directional STOP has not been verified.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from collections.abc import Mapping

from .keeson_calibrated import AXES, CalibratedKeesonController, validate_target

_LOGGER = logging.getLogger(__name__)
NATIVE_MAXIMA = {"back": 68, "legs": 44}
# Application watchdogs, not inferred protocol timing requirements.
INITIAL_FEEDBACK_TIMEOUT = 5.0
NATIVE_WRITE_TIMEOUT = 2.0
NATIVE_SEEK_TIMEOUT = 60.0
NATIVE_NO_PROGRESS_TIMEOUT = 5.0


def native_coordinate(axis: str, percent: float) -> int:
    """Quantize a percentage to the model's integer app coordinate."""
    return math.floor(validate_target(percent) * NATIVE_MAXIMA[axis] / 100 + 0.5)


def build_native_target(back: int, legs: int) -> bytes:
    """Encode the app's CustomPreset packet, including its complement checksum."""
    for axis, value in (("back", back), ("legs", legs)):
        if type(value) is not int or not 0 <= value <= NATIVE_MAXIMA[axis]:
            raise ValueError(f"Native 633: invalid {axis} coordinate")
    payload = bytes((0xE5, 0xFE, 0x17, 0, back, 0, legs))
    return payload + bytes(((~sum(payload)) & 0xFF,))


class Native633KeesonController(CalibratedKeesonController):
    """Send one absolute target, observing real notifications until completion."""

    def _decode_feedback(self, data: bytes) -> dict[str, int] | None:
        # The native app reads signed bytes 4/6 and clamps to model limits.
        # Do not apply the old raw-16-bit sentinel/maxima rejection here:
        # recorded endpoint FFFF represents a signed -1, clamped to zero.
        if len(data) != 16 or data[:3] != b"\xed\xfe\x16" or sum(data) & 0xFF != 0xFF:
            return None
        return {
            "back": int.from_bytes(data[3:5], "little"),
            "legs": int.from_bytes(data[5:7], "little"),
        }

    @staticmethod
    def _coordinates(raw: Mapping[str, int]) -> dict[str, int]:
        coordinates = {}
        for axis in AXES:
            value = (raw[axis] >> 8) & 0xFF
            signed = value - 256 if value >= 128 else value
            coordinates[axis] = max(0, min(signed, NATIVE_MAXIMA[axis]))
        return coordinates

    def _feedback_percentages(self, raw: Mapping[str, int]) -> dict[str, float]:
        return {
            axis: value * 100.0 / NATIVE_MAXIMA[axis]
            for axis, value in self._coordinates(raw).items()
        }

    async def async_feedback_seek(self, axis: str, target: float) -> None:
        if axis not in AXES:
            raise ValueError("Native 633: unsupported position axis")
        requested = validate_target(target)
        encoded = native_coordinate(axis, requested)
        session = self._feedback_session
        if session is None:
            raise ConnectionError("Native 633: position subscription is not initialized")
        self._feedback_check_link(session)
        attempted = False
        completed = False
        try:
            # No query or probe: the native app also requires a notification
            # before enabling its sliders. Never synthesize the other axis.
            if not await self._feedback_wait_feedback(axis, session, INITIAL_FEEDBACK_TIMEOUT):
                raise ConnectionError("Native 633: fresh position missing; no movement sent")
            self._feedback_check_link(session)
            if not all(self._feedback_fresh(key) for key in AXES):
                raise ConnectionError("Native 633: both positions must be fresh; no movement sent")
            coordinates = self._coordinates(self._feedback_raw)
            initial = coordinates[axis]
            if initial == encoded:
                return
            coordinates[axis] = encoded
            packet = build_native_target(coordinates["back"], coordinates["legs"])
            up = encoded > initial
            generation = self._feedback_seq
            best = initial
            progress_stamp = time.monotonic()
            self._set_motion(axis, up)
            _LOGGER.info(
                "Native 633: %s requested %.2f%%, coordinate %d; preserved pair back=%d legs=%d; one write",
                axis,
                requested,
                encoded,
                coordinates["back"],
                coordinates["legs"],
            )
            async with asyncio.timeout(NATIVE_SEEK_TIMEOUT):
                attempted = True
                try:
                    async with asyncio.timeout(NATIVE_WRITE_TIMEOUT):
                        await self.write_command(packet, repeat_count=1)
                except TimeoutError as err:
                    raise TimeoutError(
                        "Native 633: target write timed out; delivery unknown; no retry"
                    ) from err
                while True:
                    self._feedback_check_link(session)
                    if not self._feedback_fresh(axis):
                        raise ConnectionError("Native 633: live position feedback lost")
                    current = self._coordinates(self._feedback_raw)[axis]
                    if self._feedback_seq > generation:
                        if current == encoded or (current > encoded if up else current < encoded):
                            completed = True
                            _LOGGER.info(
                                "Native 633: target observed at %.2f%%",
                                self._feedback_positions[axis],
                            )
                            return
                        progress = (current - best) if up else (best - current)
                        if progress > 0:
                            best, progress_stamp = current, time.monotonic()
                        elif progress < -1:
                            raise RuntimeError("Native 633: section moved in the wrong direction")
                    if time.monotonic() - progress_stamp >= NATIVE_NO_PROGRESS_TIMEOUT:
                        raise TimeoutError("Native 633: no position progress")
                    # Existing feedback wait observes cancellation and pins the
                    # session. Capture the current generation without an await.
                    await self._feedback_wait_feedback(axis, session, 0.05, self._feedback_seq)
        finally:
            try:
                if attempted and not completed:
                    _LOGGER.warning(
                        "Native 633: requesting best-effort STOP; physical cancellation of native targets is unverified"
                    )
                    await self._feedback_stop_cleanup(session, sys.exception())
            finally:
                self._set_motion(None)
