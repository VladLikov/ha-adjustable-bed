"""Opt-in calibrated EDFE16 feedback for two-motor Keeson/Ergomotion frames.

Ported from a user-tested local implementation. Calibration is per frame;
no address, entry identity, or advertised name selects this controller.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..position_profile import PositionProfile
from .keeson import KeesonController

if TYPE_CHECKING:
    from bleak import BleakClient

    from ..coordinator import AdjustableBedCoordinator

_LOGGER = logging.getLogger(__name__)
AXES = ("back", "legs")
COMMANDS = {
    ("back", True): 0x01,
    ("back", False): 0x02,
    ("legs", True): 0x04,
    ("legs", False): 0x08,
}
ZERO_DEADBAND = 0.5
TOLERANCE = 0.5
FEEDBACK_MAX_AGE = 1.0
PASSIVE_WAIT = 0.25
PROBE_DURATION = 0.100
PROBE_FEEDBACK_WAIT = 1.2
# A captured cold start delivered its first valid report 2.056 s after the
# second STOP. Allow that delayed report without extending motor activity.
FINAL_PROBE_FEEDBACK_WAIT = 3.0
WRITE_TIMEOUT = 2.0
STEP_INTERVAL = 0.100
NO_PROGRESS_TIMEOUT = 2.5
SEEK_TIMEOUT = 60.0


def validate_target(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Calibrated position: target must be a number from 0 to 100")
    try:
        target = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("Calibrated position: target must be a number from 0 to 100") from err
    if not math.isfinite(target) or not 0 <= target <= 100:
        raise ValueError("Calibrated position: target must be finite and between 0 and 100")
    return target


def decode_frame(data: bytes, maxima: Mapping[str, int]) -> dict[str, int] | None:
    """Decode only the recorded 16-byte EDFE16 layout; reject bad snapshots."""
    if len(data) != 16 or data[:3] != b"\xed\xfe\x16":
        return None
    if sum(data) & 0xFF != 0xFF:
        return None
    raw = {"back": int.from_bytes(data[3:5], "little"), "legs": int.from_bytes(data[5:7], "little")}
    if any(v == 0xFFFF or v > maxima[a] * 1.10 for a, v in raw.items()):
        return None
    return raw


def to_percent(raw: int, maximum: int) -> float:
    p = 100.0 * raw / maximum
    if p <= ZERO_DEADBAND:
        return 0.0
    return min(100.0, max(0.0, p))


class CalibratedKeesonController(KeesonController):
    """Keeson protocol with explicitly configured raw calibration and safe seek."""

    def __init__(self, coordinator: AdjustableBedCoordinator, profile: PositionProfile) -> None:
        super().__init__(coordinator, variant="ergomotion")
        self._profile = profile
        self._feedback_session: object | None = None
        self._feedback_client: BleakClient | None = None
        self._feedback_seq = 0
        self._feedback_stamp: float | None = None
        self._feedback_positions: dict[str, float] = {}
        self._feedback_raw: dict[str, int] = {}
        self._feedback_event = asyncio.Event()
        self._feedback_rejected = 0
        self._motion_axis: str | None = None
        self._motion_up: bool | None = None

    @property
    def feedback_seek_axes(self) -> tuple[str, ...]:
        return AXES

    @property
    def position_motion(self) -> tuple[str | None, bool | None]:
        return self._motion_axis, self._motion_up

    def _set_motion(self, axis: str | None, up: bool | None = None) -> None:
        self._motion_axis, self._motion_up = axis, up
        self._coordinator.notify_position_listeners()

    async def start_notify(self, callback: Callable[[str, float], None] | None = None) -> None:
        self._notify_callback = callback
        self._feedback_client = self.client
        session = self._feedback_session = object()
        self._feedback_seq = 0
        self._feedback_stamp = None
        self._feedback_positions = {}
        self._feedback_raw = {}
        self._feedback_event = asyncio.Event()
        self._head_position = None
        self._foot_position = None
        self._coordinator.clear_position_feedback(AXES)
        if self.client is None or not self.client.is_connected:
            raise ConnectionError("Cannot subscribe to calibrated position feedback")

        def on_notification(sender: Any, data: bytearray) -> None:
            # Pin the callback itself, including re-subscription on the same client.
            if self._feedback_session is session and self.client is self._feedback_client:
                self._on_notification(sender, data)

        await self.client.start_notify(self._notify_char_uuid, on_notification)

    def _parse_position_message(self, data: bytes, msg_len: int) -> None:
        raw = decode_frame(bytes(data), self._profile.maxima)
        if raw is None:
            self._feedback_rejected = getattr(self, "_feedback_rejected", 0) + 1
            return
        # A callback from a discarded controller must never overwrite a new link.
        if self._coordinator.controller is not self:
            return
        if self.client is not getattr(self, "_feedback_client", None):
            return
        positions = {axis: to_percent(raw[axis], self._profile.maxima[axis]) for axis in AXES}
        self._feedback_raw = raw
        self._feedback_positions = positions
        self._feedback_stamp = time.monotonic()
        self._feedback_seq = getattr(self, "_feedback_seq", 0) + 1
        self._head_position = round(positions["back"])
        self._foot_position = round(positions["legs"])
        if self._notify_callback is not None:
            for axis in AXES:
                self._notify_callback(axis, positions[axis])
        self._feedback_event.set()

    def _feedback_check_link(self, session: object) -> None:
        if (
            getattr(self, "_feedback_session", None) is not session
            or self._coordinator.controller is not self
            or self.client is not getattr(self, "_feedback_client", None)
            or self.client is None
            or not self.client.is_connected
        ):
            raise ConnectionError(
                "Calibrated position: Bluetooth connection changed; position command aborted"
            )
        if self._coordinator.cancel_command.is_set():
            raise asyncio.CancelledError

    def _feedback_fresh(self, axis: str, after_seq: int | None = None) -> bool:
        stamp = getattr(self, "_feedback_stamp", None)
        return (
            stamp is not None
            and time.monotonic() - stamp <= FEEDBACK_MAX_AGE
            and axis in getattr(self, "_feedback_positions", {})
            and (after_seq is None or self._feedback_seq > after_seq)
        )

    async def _feedback_wait_feedback(
        self, axis: str, session: object, timeout: float, after_seq: int | None = None
    ) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self._feedback_check_link(session)
            if self._feedback_fresh(axis, after_seq):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # No await between checking the generation and clearing the event.
            self._feedback_event.clear()
            try:
                await asyncio.wait_for(self._feedback_event.wait(), min(0.05, remaining))
            except TimeoutError:
                pass

    async def _feedback_move_write(self, axis: str, up: bool, session: object) -> None:
        self._feedback_check_link(session)
        # Never retry a timed-out motor write: it may already have reached the bed.
        async with asyncio.timeout(WRITE_TIMEOUT):
            await self.write_command(
                self._build_command(COMMANDS[axis, up]), repeat_count=1, repeat_delay_ms=100
            )
        self._feedback_check_link(session)

    async def _feedback_stop_now(self, session: object) -> None:
        """Try STOP on the pinned link, ignoring the movement cancellation flag."""
        # No reconnect here: a late STOP must not hit a different controller/session.
        if (
            getattr(self, "_feedback_session", None) is not session
            or self._coordinator.controller is not self
            or self.client is not getattr(self, "_feedback_client", None)
            or self.client is None
            or not self.client.is_connected
        ):
            raise ConnectionError(
                "Calibrated position: STOP could not be delivered because the BLE link was lost"
            )
        self._motor_state = {}
        async with asyncio.timeout(WRITE_TIMEOUT):
            # The original Ergomotion release sends the already-used zero command
            # with a fresh cancellation event, not an invented protocol operation.
            await self._release_motion(delay=False)

    async def _feedback_stop_shielded(self, session: object) -> None:
        """Keep the command lock until bounded STOP cleanup has actually finished."""
        task = asyncio.create_task(self._feedback_stop_now(session))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _feedback_probe(self, axis: str, up: bool, session: object) -> None:
        self._set_motion(axis, up)
        try:
            await self._feedback_move_write(axis, up, session)
            # Nominal duration after the write completes, not a hardware-timed pulse.
            await asyncio.sleep(PROBE_DURATION)
        finally:
            try:
                await self._feedback_stop_shielded(session)
            finally:
                self._set_motion(None)

    async def async_feedback_seek(self, axis: str, target: float) -> None:
        """One explicit operation: acquire feedback, then position with watchdogs."""
        if axis not in AXES:
            raise ValueError("Calibrated position: only back/head and legs/feet are calibrated")
        target = validate_target(target)
        if not getattr(self, "_feedback_event", None):
            raise ConnectionError(
                "Calibrated position: notification subscription is not initialized"
            )
        session = self._feedback_session
        if session is None:
            raise ConnectionError("Position subscription is not initialized")
        self._feedback_check_link(session)
        movement_attempted = False
        acquisition_started = time.monotonic()
        _LOGGER.info("Calibrated position: requested %s target %.2f%%", axis, target)
        try:
            async with asyncio.timeout(SEEK_TIMEOUT):
                fresh = await self._feedback_wait_feedback(axis, session, PASSIVE_WAIT)
                if not fresh:
                    if not self._profile.allow_motion_probe:
                        raise ConnectionError(
                            "Calibrated position: fresh position missing; motion probe is disabled"
                        )
                    # At most two bounded pulses. Prefer the requested endpoint's
                    # direction; one opposite pulse covers a non-reporting end stop.
                    first_up = target > 50.0
                    for probe_index, up in enumerate((first_up, not first_up)):
                        self._feedback_check_link(session)
                        if self._feedback_fresh(axis):
                            fresh = True
                            break
                        previous_seq = self._feedback_seq
                        movement_attempted = True
                        _LOGGER.info(
                            "Calibrated position: bounded probe %s %s (nominal 100 ms)",
                            axis,
                            "up" if up else "down",
                        )
                        await self._feedback_probe(axis, up, session)
                        wait = (
                            PROBE_FEEDBACK_WAIT if probe_index == 0 else FINAL_PROBE_FEEDBACK_WAIT
                        )
                        _LOGGER.debug(
                            "Calibrated position: %s probe %d stopped; awaiting feedback up to %.2f s",
                            axis,
                            probe_index + 1,
                            wait,
                        )
                        fresh = await self._feedback_wait_feedback(
                            axis, session, wait, previous_seq
                        )
                        if fresh:
                            break
                    if not fresh:
                        _LOGGER.warning(
                            "Calibrated position: %s target %.2f%% acquisition failed after %.3f s",
                            axis,
                            target,
                            time.monotonic() - acquisition_started,
                        )
                        raise ConnectionError(
                            "Calibrated position: no valid position after two bounded probes; stopped"
                        )

                self._feedback_check_link(session)
                if not self._feedback_fresh(axis):
                    raise ConnectionError(
                        "Calibrated position: feedback became stale before movement"
                    )
                current = self._feedback_positions[axis]
                _LOGGER.info(
                    "Calibrated position: %s feedback acquired at %.2f%% after %.3f s (target %.2f%%)",
                    axis,
                    current,
                    time.monotonic() - acquisition_started,
                    target,
                )
                if abs(target - current) <= TOLERANCE:
                    return
                up = target > current
                best = current
                last_progress = time.monotonic()
                self._set_motion(axis, up)
                _LOGGER.info("Calibrated position: %s %.2f%% -> %.2f%%", axis, current, target)
                while True:
                    self._feedback_check_link(session)
                    if not self._feedback_fresh(axis):
                        raise ConnectionError(
                            "Calibrated position: live position feedback lost; stopped"
                        )
                    current = self._feedback_positions[axis]
                    if (
                        abs(target - current) <= TOLERANCE
                        or (up and current >= target)
                        or (not up and current <= target)
                    ):
                        break
                    progress = (current - best) if up else (best - current)
                    if progress >= 0.1:
                        best, last_progress = current, time.monotonic()
                    elif progress < -1.0:
                        raise RuntimeError(
                            "Calibrated position: section moved in the wrong direction; stopped"
                        )
                    elif time.monotonic() - last_progress >= NO_PROGRESS_TIMEOUT:
                        raise RuntimeError("Calibrated position: section made no progress; stopped")
                    movement_attempted = True
                    await self._feedback_move_write(axis, up, session)
                    await asyncio.sleep(STEP_INTERVAL)
                # Never fabricate exact completion by publishing the requested value.
                _LOGGER.info(
                    "Calibrated position: %s stopped at measured %.2f%% (target %.2f%%)",
                    axis,
                    current,
                    target,
                )
        except TimeoutError as err:
            raise TimeoutError(
                "Calibrated position: position operation timed out; STOP requested"
            ) from err
        finally:
            try:
                if movement_attempted:
                    await self._feedback_stop_shielded(session)
            finally:
                self._set_motion(None)
