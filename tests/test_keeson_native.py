"""Native 633 packets and one-shot movement lifecycle, without physical BLE."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.adjustable_bed.beds import keeson_native as module
from custom_components.adjustable_bed.beds.keeson_native import (
    Native633KeesonController,
    build_native_target,
    native_coordinate,
)
from custom_components.adjustable_bed.position_profile import PositionProfile
from tests.test_keeson_calibrated import frame, notify, subscribe


@pytest.fixture
def rig(monkeypatch):
    client = MagicMock(is_connected=True)
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.services = []
    coordinator = MagicMock()
    coordinator.client = client
    coordinator.cancel_command = asyncio.Event()
    coordinator.position_data = {}
    ctrl = Native633KeesonController(coordinator, PositionProfile(17700, 11586, True, True))
    coordinator.controller = ctrl
    monkeypatch.setattr(module, "INITIAL_FEEDBACK_TIMEOUT", 0.03)
    monkeypatch.setattr(module, "NATIVE_WRITE_TIMEOUT", 0.03)
    monkeypatch.setattr(module, "NATIVE_NO_PROGRESS_TIMEOUT", 0.06)
    monkeypatch.setattr(module, "NATIVE_SEEK_TIMEOUT", 0.2)
    return SimpleNamespace(ctrl=ctrl, client=client, coordinator=coordinator)


def test_frozen_app_vectors():
    assert build_native_target(0, 0).hex() == "e5fe170000000005"
    assert build_native_target(10, 20).hex() == "e5fe17000a0014e7"
    assert build_native_target(68, 44).hex() == "e5fe170044002c95"


@pytest.mark.parametrize("pair", [(True, 0), (0, False), (1.5, 0), (-1, 0), (69, 0), (0, 45)])
def test_reject_bad_coordinates(pair):
    with pytest.raises(ValueError):
        build_native_target(*pair)


@pytest.mark.parametrize(
    "axis,percent,expected",
    [
        ("back", 0, 0),
        ("legs", 100, 44),
        ("back", 100, 68),
        ("back", 50, 34),
        ("legs", 50, 22),
        ("legs", 51, 22),
    ],
)
def test_quantization(axis, percent, expected):
    assert native_coordinate(axis, percent) == expected


@pytest.mark.parametrize(
    "axis,back,legs,packet",
    [
        ("legs", 10, 0, "e5fe17000a0016e5"),
        ("back", 0, 20, "e5fe1700220014cf"),
    ],
)
async def test_one_real_gatt_write_preserves_other_axis(rig, axis, back, legs, packet):
    await subscribe(rig)
    notify(rig, back << 8, legs << 8)
    before = dict(rig.coordinator.position_data)

    async def write(uuid, payload, response):
        assert payload.hex() == packet
        assert response is True
        assert rig.coordinator.position_data == before  # No optimistic target.
        notify(rig, (34 if axis == "back" else back) << 8, (22 if axis == "legs" else legs) << 8)
        await asyncio.sleep(0.001)  # Notification may precede the GATT ack.

    rig.client.write_gatt_char.side_effect = write
    with patch.object(rig.ctrl, "_feedback_stop_cleanup", new_callable=AsyncMock) as stop:
        await rig.ctrl.async_feedback_seek(axis, 50)
        stop.assert_not_awaited()
    rig.client.write_gatt_char.assert_awaited_once()
    assert rig.coordinator.position_data[axis] == 50
    assert rig.ctrl.position_motion == (None, None)


async def test_native_feedback_ignores_old_raw_calibration(rig):
    await subscribe(rig)
    notify(rig, (34 << 8) + 240, (22 << 8) + 199)
    assert rig.ctrl._feedback_positions == {"back": 50, "legs": 50}
    notify(rig, 68 << 8, 44 << 8)
    assert rig.ctrl._feedback_positions == {"back": 100, "legs": 100}


@pytest.mark.parametrize("stale", [False, True])
async def test_missing_feedback_sends_nothing_even_with_probes_enabled(rig, stale):
    await subscribe(rig)
    if stale:
        notify(rig, 0, 0)
        rig.ctrl._feedback_stamp = time.monotonic() - 10
    with pytest.raises(ConnectionError, match="no movement sent"):
        await rig.ctrl.async_feedback_seek("legs", 50)
    rig.client.write_gatt_char.assert_not_awaited()


async def test_cold_notification_enables_one_target(rig):
    await subscribe(rig)
    task = asyncio.create_task(rig.ctrl.async_feedback_seek("legs", 50))
    await asyncio.sleep(0.005)
    rig.client.write_gatt_char.assert_not_awaited()
    notify(rig, 10 << 8, 0)
    rig.client.write_gatt_char.side_effect = lambda *a, **kw: notify(rig, 10 << 8, 22 << 8)
    await task
    rig.client.write_gatt_char.assert_awaited_once()


async def test_old_session_callback_cannot_enable_target(rig):
    await subscribe(rig)
    old = rig.client.start_notify.call_args.args[1]
    await subscribe(rig)
    old(None, bytearray(frame(0, 0)))
    with pytest.raises(ConnectionError):
        await rig.ctrl.async_feedback_seek("legs", 50)
    rig.client.write_gatt_char.assert_not_awaited()


async def test_matching_quantized_target_needs_no_write(rig):
    await subscribe(rig)
    notify(rig, 10 << 8, 22 << 8)
    await rig.ctrl.async_feedback_seek("legs", 51)
    rig.client.write_gatt_char.assert_not_awaited()
    assert rig.ctrl._feedback_positions["legs"] == 50


async def test_write_timeout_no_retry_preserves_primary_error(rig):
    await subscribe(rig)
    notify(rig, 0, 0)

    async def delayed(*args, **kwargs):
        await asyncio.sleep(1)

    rig.client.write_gatt_char.side_effect = delayed
    with patch.object(
        rig.ctrl,
        "_feedback_stop_shielded",
        new_callable=AsyncMock,
        side_effect=ConnectionError("STOP failed"),
    ) as stop:
        with pytest.raises(TimeoutError, match="delivery unknown; no retry") as error:
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()
    assert error.value.__notes__
    rig.client.write_gatt_char.assert_awaited_once()
    assert rig.ctrl.position_motion == (None, None)


async def test_cancel_pending_write_requests_stop_once(rig):
    await subscribe(rig)
    notify(rig, 0, 0)
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    rig.client.write_gatt_char.side_effect = pending
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        task = asyncio.create_task(rig.ctrl.async_feedback_seek("legs", 50))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stop.assert_awaited_once()
    rig.client.write_gatt_char.assert_awaited_once()


async def test_no_progress_does_not_resend_target(rig):
    await subscribe(rig)
    notify(rig, 0, 0)
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        with pytest.raises(TimeoutError, match="no position progress"):
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()
    rig.client.write_gatt_char.assert_awaited_once()


def test_native_profile_explicit_boolean():
    assert PositionProfile(17700, 11586).native_633_position is False
    with pytest.raises(ValueError, match="Native 633"):
        PositionProfile(17700, 11586, False, "yes")


async def test_link_change_aborts_without_target_retry_or_stop_on_new_link(rig):
    await subscribe(rig)
    notify(rig, 0, 0)
    replacement = MagicMock(is_connected=True)
    replacement.write_gatt_char = AsyncMock()

    def replace(*args, **kwargs):
        rig.coordinator.client = replacement

    rig.client.write_gatt_char.side_effect = replace
    with pytest.raises(ConnectionError, match="connection changed"):
        await rig.ctrl.async_feedback_seek("legs", 50)
    rig.client.write_gatt_char.assert_awaited_once()
    replacement.write_gatt_char.assert_not_awaited()


async def test_feedback_loss_after_ack_requests_stop(rig):
    await subscribe(rig)
    notify(rig, 0, 0)

    def stale(*args, **kwargs):
        rig.ctrl._feedback_stamp = time.monotonic() - 10

    rig.client.write_gatt_char.side_effect = stale
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        with pytest.raises(ConnectionError, match="feedback lost"):
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()
    rig.client.write_gatt_char.assert_awaited_once()


async def test_cancel_event_while_observing_requests_stop(rig):
    await subscribe(rig)
    notify(rig, 0, 0)

    def cancel(*args, **kwargs):
        rig.coordinator.cancel_command.set()

    rig.client.write_gatt_char.side_effect = cancel
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        with pytest.raises(asyncio.CancelledError):
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()


async def test_wrong_direction_is_not_success(rig):
    await subscribe(rig)
    notify(rig, 0, 10 << 8)
    rig.client.write_gatt_char.side_effect = lambda *a, **kw: notify(rig, 0, 7 << 8)
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        with pytest.raises(RuntimeError, match="wrong direction"):
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()


async def test_total_watchdog_with_valid_stationary_feedback(rig, monkeypatch):
    monkeypatch.setattr(module, "NATIVE_SEEK_TIMEOUT", 0.01)
    await subscribe(rig)
    notify(rig, 0, 0)
    with patch.object(rig.ctrl, "_feedback_stop_shielded", new_callable=AsyncMock) as stop:
        with pytest.raises(TimeoutError):
            await rig.ctrl.async_feedback_seek("legs", 50)
        stop.assert_awaited_once()
    rig.client.write_gatt_char.assert_awaited_once()


async def test_native_bad_checksum_cannot_refresh_feedback(rig):
    await subscribe(rig)
    raw = bytearray(frame(0, 0))
    raw[-1] ^= 1
    rig.client.start_notify.call_args.args[1](None, raw)
    assert rig.ctrl._feedback_positions == {}
    assert rig.ctrl._feedback_seq == 0


async def test_recorded_negative_endpoint_after_reconnect_can_start_target(rig):
    await subscribe(rig)
    packet = bytes.fromhex("edfe160000ffff0000000000000ffff2")
    rig.client.start_notify.call_args.args[1](None, bytearray(packet))
    assert rig.ctrl._feedback_positions == {"back": 0.0, "legs": 0.0}
    rig.client.write_gatt_char.side_effect = lambda *a, **kw: notify(rig, 0, 22 << 8)
    await rig.ctrl.async_feedback_seek("legs", 50)
    rig.client.write_gatt_char.assert_awaited_once()
    assert rig.client.write_gatt_char.call_args.args[1].hex() == "e5fe1700000016ef"


@pytest.mark.parametrize(
    "raw,expected", [(0xFFFF, 0), (0xFF00, 0), (0x8000, 0), (0x7FFF, 100), (0x2C00, 100)]
)
async def test_native_signed_coordinate_clamping(rig, raw, expected):
    await subscribe(rig)
    notify(rig, 0, raw)
    assert rig.ctrl._feedback_positions["legs"] == expected
