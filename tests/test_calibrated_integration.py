"""Real coordinator lifecycle, native cover and options flow for calibrated frames."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.beds.keeson_calibrated import CalibratedKeesonController
from custom_components.adjustable_bed.const import DOMAIN
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator
from custom_components.adjustable_bed.cover import COVER_DESCRIPTIONS, AdjustableBedCover
from custom_components.adjustable_bed.position_profile import (
    CONF_ALLOW_MOTION_PROBE,
    CONF_BACK_RAW_MAX,
    CONF_CALIBRATED_POSITION,
    CONF_LEGS_RAW_MAX,
    CONF_NATIVE_633_POSITION,
)


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Calibrated frame",
        data={
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Calibrated frame",
            "bed_type": "keeson",
            "protocol_variant": "ergomotion",
            "motor_count": 2,
            "disable_angle_sensing": False,
            CONF_CALIBRATED_POSITION: True,
            CONF_BACK_RAW_MAX: 17700,
            CONF_LEGS_RAW_MAX: 11586,
            CONF_ALLOW_MOTION_PROBE: True,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_factory_native_cover_and_reconnect_capability(
    hass, entry, mock_coordinator_connected
):
    coordinator = AdjustableBedCoordinator(hass, entry)
    await coordinator.async_connect()
    assert isinstance(coordinator.controller, CalibratedKeesonController)
    assert coordinator.feedback_seek_axes == ("back", "legs")
    cover = AdjustableBedCover(coordinator, next(d for d in COVER_DESCRIPTIONS if d.key == "back"))
    assert cover.supported_features == (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    coordinator._position_data["back"] = 50
    assert cover.current_cover_position == 50
    with patch.object(coordinator, "async_seek_position", new_callable=AsyncMock) as seek:
        await cover.async_set_cover_position(position=30)
        assert seek.call_args.args[:2] == ("back", 30)
        await cover.async_open_cover()
        assert seek.call_args.args[:2] == ("back", 100)
        await cover.async_close_cover()
        assert seek.call_args.args[:2] == ("back", 0)
    with patch.object(coordinator, "async_stop_command", new_callable=AsyncMock) as stop:
        await cover.async_stop_cover()
        stop.assert_awaited_once()
    await coordinator.async_disconnect()
    assert cover.current_cover_position is None
    assert cover.is_closed is None
    assert coordinator.feedback_seek_axes == ("back", "legs")


async def test_stop_while_connecting_never_starts_seek(hass, entry):
    coordinator = AdjustableBedCoordinator(hass, entry)
    coordinator._feedback_seek_axes = ("back", "legs")
    entered = asyncio.Event()
    release = asyncio.Event()
    controller = MagicMock()
    controller.feedback_seek_axes = ("back", "legs")
    controller.async_feedback_seek = AsyncMock()
    controller.stop_all = AsyncMock()

    async def connect(**kwargs):
        entered.set()
        await release.wait()
        coordinator._controller = controller
        return True

    with (
        patch.object(coordinator, "async_ensure_connected", side_effect=connect),
        patch.object(coordinator, "_async_refresh_controller_auth", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(coordinator.async_seek_position("back", 50, None, None, None))
        await entered.wait()
        stop = asyncio.create_task(coordinator.async_stop_command())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, stop)
    controller.async_feedback_seek.assert_not_awaited()
    controller.stop_all.assert_awaited_once()


async def test_new_target_waits_for_old_seek_cleanup(hass, entry):
    coordinator = AdjustableBedCoordinator(hass, entry)
    coordinator._feedback_seek_axes = ("back", "legs")
    started = asyncio.Event()
    cleanup = asyncio.Event()
    release = asyncio.Event()
    events = []
    controller = MagicMock()
    controller.feedback_seek_axes = ("back", "legs")
    coordinator._controller = controller

    async def seek(axis, target):
        events.append(target)
        if target == 50:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                events.append("stop")

    controller.async_feedback_seek = AsyncMock(side_effect=seek)

    async def prepare(name):
        coordinator._cancel_command.clear()
        return controller

    with patch.object(coordinator, "_async_prepare_controller_operation", side_effect=prepare):
        first = asyncio.create_task(coordinator.async_seek_position("back", 50, None, None, None))
        await started.wait()
        second = asyncio.create_task(coordinator.async_seek_position("back", 25, None, None, None))
        await cleanup.wait()
        assert events == [50]
        release.set()
        await asyncio.gather(first, second)
    assert events == [50, "stop", 25]


@pytest.mark.parametrize("maximum,valid", [(0, False), (17700, True)])
async def test_options_validate_and_preserve_calibration(hass, entry, maximum, valid, enable_custom_integrations):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_CALIBRATED_POSITION in keys
    assert CONF_NATIVE_633_POSITION in keys
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bed_type": "keeson",
            "protocol_variant": "ergomotion",
            "motor_count": 2,
            "disable_angle_sensing": False,
            CONF_CALIBRATED_POSITION: True,
            CONF_NATIVE_633_POSITION: True,
            CONF_BACK_RAW_MAX: maximum,
            CONF_LEGS_RAW_MAX: 11586,
            CONF_ALLOW_MOTION_PROBE: True,
        },
    )
    if valid:
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert entry.data[CONF_BACK_RAW_MAX] == 17700
        assert entry.data[CONF_NATIVE_633_POSITION] is True
    else:
        assert result["errors"] == {"base": "invalid_position_profile"}


async def test_cover_callbacks_are_removed(hass, entry, mock_coordinator_connected):
    coordinator = AdjustableBedCoordinator(hass, entry)
    await coordinator.async_connect()
    cover = AdjustableBedCover(coordinator, next(d for d in COVER_DESCRIPTIONS if d.key == "legs"))
    cover.hass = hass
    cover.entity_id = "cover.test_legs"
    before = len(coordinator._position_callbacks)
    with patch.object(cover, "async_write_ha_state") as update:
        await cover.async_added_to_hass()
        assert len(coordinator._position_callbacks) == before + 1
        coordinator.notify_position_listeners()
        assert update.called
        await cover.async_remove(force_remove=True)
        assert len(coordinator._position_callbacks) == before
    await coordinator.async_disconnect()


@pytest.mark.parametrize("stage", ["queue", "connect", "auth"])
async def test_expired_position_never_starts_after_slow_preparation(hass, entry, monkeypatch, stage):
    from custom_components.adjustable_bed import coordinator as module

    monkeypatch.setattr(module, "CALIBRATED_COMMAND_START_TIMEOUT", 0.02)
    coordinator = AdjustableBedCoordinator(hass, entry)
    coordinator._feedback_seek_axes = ("back", "legs")
    controller = MagicMock()
    controller.feedback_seek_axes = ("back", "legs")
    controller.async_feedback_seek = AsyncMock()
    connect_completed = asyncio.Event()

    async def connect(**kwargs):
        if stage == "connect":
            await asyncio.sleep(0.04)
        coordinator._controller = controller
        connect_completed.set()
        return True

    async def auth():
        if stage == "auth":
            await asyncio.sleep(0.04)

    if stage == "queue":
        await coordinator._command_lock.acquire()
    with (
        patch.object(coordinator, "async_ensure_connected", side_effect=connect) as ensure,
        patch.object(coordinator, "_async_refresh_controller_auth", side_effect=auth),
    ):
        task = asyncio.create_task(coordinator.async_seek_position("legs", 50, None, None, None))
        if stage == "queue":
            await asyncio.sleep(0.04)
            coordinator._command_lock.release()
        with pytest.raises(TimeoutError, match="expired"):
            await task
        controller.async_feedback_seek.assert_not_awaited()
        if stage == "queue":
            ensure.assert_not_awaited()
        else:
            assert connect_completed.is_set()  # Transport cleanup was not interrupted.

        # Only a fresh explicit command may move after the link becomes usable.
        monkeypatch.setattr(module, "CALIBRATED_COMMAND_START_TIMEOUT", 1)
        await coordinator.async_seek_position("legs", 25, None, None, None)
        controller.async_feedback_seek.assert_awaited_once_with("legs", 25)
    assert not coordinator._command_lock.locked()


async def test_start_expiry_does_not_interrupt_an_active_seek(hass, entry, monkeypatch):
    from custom_components.adjustable_bed import coordinator as module

    monkeypatch.setattr(module, "CALIBRATED_COMMAND_START_TIMEOUT", 0.03)
    coordinator = AdjustableBedCoordinator(hass, entry)
    coordinator._feedback_seek_axes = ("back", "legs")
    controller = MagicMock()
    controller.feedback_seek_axes = ("back", "legs")
    completed = asyncio.Event()

    async def seek(axis, target):
        await asyncio.sleep(0.06)
        completed.set()

    controller.async_feedback_seek = AsyncMock(side_effect=seek)

    async def prepare(name):
        coordinator._cancel_command.clear()
        return controller

    with patch.object(coordinator, "_async_prepare_controller_operation", side_effect=prepare):
        await coordinator.async_seek_position("legs", 50, None, None, None)
    assert completed.is_set()
    controller.async_feedback_seek.assert_awaited_once()


async def test_factory_native_633_is_explicit(hass, entry, mock_coordinator_connected):
    from custom_components.adjustable_bed.beds.keeson_native import Native633KeesonController
    from custom_components.adjustable_bed.position_profile import CONF_NATIVE_633_POSITION

    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_NATIVE_633_POSITION: True})
    coordinator = AdjustableBedCoordinator(hass, entry)
    await coordinator.async_connect()
    assert isinstance(coordinator.controller, Native633KeesonController)
    assert coordinator.feedback_seek_axes == ("back", "legs")
    await coordinator.async_disconnect()
