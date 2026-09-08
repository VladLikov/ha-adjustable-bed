"""Calibration, stale-session isolation and safety of controller-owned position seek."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.adjustable_bed.beds import keeson_calibrated as module
from custom_components.adjustable_bed.beds.keeson_calibrated import (
    CalibratedKeesonController,
    decode_frame,
    to_percent,
    validate_target,
)
from custom_components.adjustable_bed.position_profile import PositionProfile


def frame(back=0, legs=0):
    data = bytearray(b"\xed\xfe\x16")
    data += back.to_bytes(2, "little") + legs.to_bytes(2, "little") + bytes(8)
    data.append((255 - sum(data)) & 255)
    return bytes(data)


@pytest.fixture
def rig():
    client = MagicMock(is_connected=True)
    client.start_notify = AsyncMock()
    client.services = []
    coordinator = MagicMock()
    coordinator.client = client
    coordinator.cancel_command = asyncio.Event()
    coordinator.name = "Test frame"
    coordinator.position_data = {}
    ctrl = CalibratedKeesonController(coordinator, PositionProfile(17700, 11586, True))
    coordinator.controller = ctrl
    return SimpleNamespace(ctrl=ctrl, coordinator=coordinator, client=client)


async def subscribe(rig):
    await rig.ctrl.start_notify(
        lambda axis, value: rig.coordinator.position_data.update({axis: value})
    )


def notify(rig, back=0, legs=0):
    rig.client.start_notify.call_args.args[1](None, bytearray(frame(back, legs)))


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(module, "PASSIVE_WAIT", 0.002)
    monkeypatch.setattr(module, "PROBE_DURATION", 0.002)
    monkeypatch.setattr(module, "PROBE_FEEDBACK_WAIT", 0.004)
    monkeypatch.setattr(module, "FINAL_PROBE_FEEDBACK_WAIT", 0.012)
    monkeypatch.setattr(module, "STEP_INTERVAL", 0.002)
    monkeypatch.setattr(module, "WRITE_TIMEOUT", 0.05)
    monkeypatch.setattr(module, "SEEK_TIMEOUT", 0.5)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 101, True, None, "bad"])
def test_invalid_target(value):
    with pytest.raises(ValueError):
        validate_target(value)


@pytest.mark.parametrize("values", [(0, 10), (-1, 10), (65535, 10), (True, 10), (1.5, 10)])
def test_invalid_calibration(values):
    with pytest.raises(ValueError):
        PositionProfile(*values)


def test_decode_and_normalize():
    maxima = PositionProfile(17700, 11586).maxima
    assert decode_frame(frame(8850, 5793), maxima) == {"back": 8850, "legs": 5793}
    assert to_percent(8850, 17700) == 50
    assert to_percent(56, 17700) == 0
    assert to_percent(18000, 17700) == 100
    assert decode_frame(frame(65535, 0), maxima) is None
    assert decode_frame(frame(20000, 0), maxima) is None
    assert decode_frame(frame()[:-1], maxima) is None
    assert decode_frame(b"\x00" + frame()[1:], maxima) is None
    assert decode_frame(frame()[:-1] + b"\x00", maxima) is None


async def test_subscribe_and_background_reads_never_move(rig):
    with patch.object(rig.ctrl, "write_command", new_callable=AsyncMock) as write:
        await subscribe(rig)
        await rig.ctrl.read_positions()
        write.assert_not_awaited()
    rig.coordinator.clear_position_feedback.assert_called_once_with(("back", "legs"))


async def test_old_callback_same_client_cannot_refresh_new_subscription(rig):
    await subscribe(rig)
    old = rig.client.start_notify.call_args.args[1]
    notify(rig, 8850, 5793)
    await subscribe(rig)
    old(None, bytearray(frame(17700, 11586)))
    assert rig.ctrl._feedback_positions == {}
    notify(rig, 8850, 5793)
    assert rig.ctrl._feedback_positions == {"back": 50, "legs": 50}


async def test_discarded_controller_cannot_publish(rig):
    await subscribe(rig)
    rig.coordinator.controller = object()
    notify(rig, 8850, 5793)
    assert rig.ctrl._feedback_positions == {}


@pytest.mark.parametrize("axis", ["back", "legs"])
async def test_cold_start_acquires_feedback_and_stops_at_target(rig, axis):
    await subscribe(rig)
    commands = []
    current = 0

    async def write(command, **kwargs):
        nonlocal current
        key = int.from_bytes(command[3:7], "little")
        commands.append(key)
        if key:
            current = min(50, current + 10)
            notify(
                rig,
                round(current * 177) if axis == "back" else 0,
                round(current * 115.86) if axis == "legs" else 0,
            )

    with patch.object(rig.ctrl, "write_command", side_effect=write):
        await rig.ctrl.async_feedback_seek(axis, 50)
    assert commands[-1] == 0
    assert abs(rig.ctrl._feedback_positions[axis] - 50) < 0.1
    assert rig.ctrl._feedback_positions["legs" if axis == "back" else "back"] == 0
    assert rig.ctrl.position_motion == (None, None)


async def test_maximum_two_probes_no_blind_seek(rig):
    await subscribe(rig)
    with (
        patch.object(rig.ctrl, "write_command", new_callable=AsyncMock) as write,
        pytest.raises(ConnectionError, match="two bounded probes"),
    ):
        await rig.ctrl.async_feedback_seek("back", 50)
    commands = [int.from_bytes(c.args[0][3:7], "little") for c in write.call_args_list]
    assert [v for v in commands if v] == [2, 1]
    assert commands[-1] == 0


async def test_probe_requires_opt_in(rig):
    rig.ctrl._profile = PositionProfile(17700, 11586, False)
    await subscribe(rig)
    with patch.object(rig.ctrl, "write_command", new_callable=AsyncMock) as write:
        with pytest.raises(ConnectionError, match="disabled"):
            await rig.ctrl.async_feedback_seek("back", 50)
        write.assert_not_awaited()


async def test_cancel_during_probe_sends_stop_no_more_movement(rig, monkeypatch):
    monkeypatch.setattr(module, "PROBE_DURATION", 1)
    await subscribe(rig)
    started = asyncio.Event()
    commands = []

    async def write(command, **kwargs):
        commands.append(int.from_bytes(command[3:7], "little"))
        if commands[-1]:
            started.set()

    with patch.object(rig.ctrl, "write_command", side_effect=write):
        task = asyncio.create_task(rig.ctrl.async_feedback_seek("legs", 75))
        await started.wait()
        rig.coordinator.cancel_command.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len([v for v in commands if v]) == 1
    assert commands[-1] == 0


async def test_stale_cache_requires_fresh_feedback(rig):
    await subscribe(rig)
    notify(rig, 8850, 0)
    rig.ctrl._feedback_stamp = time.monotonic() - 2
    with (
        patch.object(rig.ctrl, "write_command", new_callable=AsyncMock) as write,
        pytest.raises(ConnectionError),
    ):
        await rig.ctrl.async_feedback_seek("back", 50)
    assert any(int.from_bytes(c.args[0][3:7], "little") for c in write.call_args_list)


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_direction",
        "no_progress",
        "feedback_loss",
        "write_timeout",
        "seek_timeout",
        "connection_loss",
    ],
)
async def test_watchdogs_stop_without_retry(rig, monkeypatch, failure):
    await subscribe(rig)
    notify(rig, 8850, 0)
    commands = []
    monkeypatch.setattr(module, "NO_PROGRESS_TIMEOUT", 0.01)
    if failure == "seek_timeout":
        monkeypatch.setattr(module, "SEEK_TIMEOUT", 0.008)
        monkeypatch.setattr(module, "NO_PROGRESS_TIMEOUT", 1)

    async def write(command, **kwargs):
        key = int.from_bytes(command[3:7], "little")
        commands.append(key)
        if not key:
            return
        if failure == "wrong_direction":
            notify(rig, 8000, 0)
        elif failure == "feedback_loss":
            rig.ctrl._feedback_stamp = time.monotonic() - 2
        elif failure == "write_timeout":
            await asyncio.sleep(1)
        elif failure == "connection_loss":
            rig.client.is_connected = False
        else:
            notify(rig, 8850, 0)

    with (
        patch.object(rig.ctrl, "write_command", side_effect=write),
        pytest.raises((RuntimeError, ConnectionError, TimeoutError)),
    ):
        await rig.ctrl.async_feedback_seek("back", 75)
    if failure == "connection_loss":
        assert len(commands) == 1  # no reconnect to another link for cleanup
    else:
        assert commands[-1] == 0
    if failure == "write_timeout":
        assert len([v for v in commands if v]) == 1


async def test_repeated_cancel_waits_for_stop_cleanup(rig):
    await subscribe(rig)
    started = asyncio.Event()
    release = asyncio.Event()

    async def stop(session):
        started.set()
        await release.wait()

    with patch.object(rig.ctrl, "_feedback_stop_now", side_effect=stop):
        task = asyncio.create_task(rig.ctrl._feedback_stop_shielded(rig.ctrl._feedback_session))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_delayed_first_feedback_after_second_stop_reaches_target(rig, monkeypatch):
    """Replay the observed 2.056 s delay, beyond the old 1.2 s deadline."""
    monkeypatch.setattr(module, "PROBE_FEEDBACK_WAIT", 1.2)
    monkeypatch.setattr(module, "FINAL_PROBE_FEEDBACK_WAIT", 3.0)
    monkeypatch.setattr(module, "SEEK_TIMEOUT", 10)
    await subscribe(rig)
    commands = []
    second_stop = asyncio.Event()
    feedback_delivered = False

    async def write(command, **kwargs):
        key = int.from_bytes(command[3:7], "little")
        commands.append(key)
        if commands == [8, 0, 4, 0]:
            second_stop.set()
        elif key and feedback_delivered:
            assert key == 4
            notify(rig, legs=5793)

    async def delayed_feedback():
        nonlocal feedback_delivered
        await second_stop.wait()
        await asyncio.sleep(2.056)
        # Only two probes and their STOPs while awaiting the initial report.
        assert commands == [8, 0, 4, 0]
        assert rig.ctrl.position_motion == (None, None)
        notify(rig, legs=42)
        feedback_delivered = True

    with patch.object(rig.ctrl, "write_command", side_effect=write):
        async with asyncio.TaskGroup() as group:
            group.create_task(delayed_feedback())
            group.create_task(rig.ctrl.async_feedback_seek("legs", 50))
    assert commands == [8, 0, 4, 0, 4, 0]
    assert rig.coordinator.position_data["legs"] == 50
    assert rig.ctrl.position_motion == (None, None)


@pytest.mark.parametrize("failure", ["cancel", "client", "session", "stale"])
async def test_final_passive_wait_preserves_safety_guards(rig, monkeypatch, failure):
    monkeypatch.setattr(module, "FINAL_PROBE_FEEDBACK_WAIT", 0.15)
    await subscribe(rig)
    commands = []
    second_stop = asyncio.Event()

    async def write(command, **kwargs):
        commands.append(int.from_bytes(command[3:7], "little"))
        if commands == [8, 0, 4, 0]:
            second_stop.set()

    with patch.object(rig.ctrl, "write_command", side_effect=write):
        task = asyncio.create_task(rig.ctrl.async_feedback_seek("legs", 50))
        try:
            await asyncio.wait_for(second_stop.wait(), 1)
            await asyncio.sleep(0.02)
            assert not task.done()
            assert commands == [8, 0, 4, 0]
            if failure == "cancel":
                rig.coordinator.cancel_command.set()
            elif failure == "client":
                rig.coordinator.client = MagicMock(is_connected=True)
            elif failure == "session":
                await subscribe(rig)
            else:
                notify(rig, legs=5793)
                rig.ctrl._feedback_stamp = time.monotonic() - 2
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else ConnectionError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert [v for v in commands if v] == [8, 4]
    if failure in ("cancel", "stale"):
        assert commands[-1] == 0
    else:
        assert commands == [8, 0, 4, 0]  # Never send cleanup onto a replaced link.
    assert rig.ctrl.position_motion == (None, None)
