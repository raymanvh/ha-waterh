"""Bluetooth through Home Assistant: session safety rules, sync manager, config flow."""

# The bottle's clock is naive local time, so these tests use naive datetimes.
# ruff: noqa: DTZ001

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.waterh import bluetooth_sync
from custom_components.waterh.ble_session import BottleSession, BottleSyncError
from custom_components.waterh.const import DOMAIN
from custom_components.waterh.protocol import (
    REQUEST_RECALIBRATE_EMPTY,
    REQUEST_STATUS,
    REQUEST_WATER_LOGS,
    LogRecord,
    clear_records,
)

ADDRESS = "AA:BB:CC:11:22:33"
# Real status packet: goal 4347 mL, today 2575 mL, reminder set, clock 2026-11-17 23:00:02.
REAL_STATUS = bytes.fromhex(
    "5250002702026307031A0B1117000203040A0F030510FB051000730011021701072601090001003C022A01"
)


def _record(when: datetime, ml: int, flag: int = 0) -> bytes:
    return bytes(
        [when.year % 100, when.month, when.day, when.hour, when.minute, when.second,
         ml >> 8, ml & 0xFF, 0, 5, 2, 0, flag]
    )


def _status_with_clock(now: datetime) -> bytes:
    """REAL_STATUS with the clock (field 3) replaced by [now]."""
    data = bytearray(REAL_STATUS)
    data[9:15] = bytes([now.year % 100, now.month, now.day, now.hour, now.minute, now.second])
    return bytes(data)


class FakeBottle:
    """Answers commands like a Boost bottle, logging every write in order."""

    def __init__(
        self,
        records: list[bytes] | None = None,
        status: bytes | None = REAL_STATUS,
        stop_after_first_packet: bool = False,
        ignore_logs_requests: int = 0,
        write_delay: float = 0,
        recalibrate_codes: tuple[int, ...] = (2, 3),
    ) -> None:
        self.recalibrate_codes = recalibrate_codes
        self.ignore_logs_requests = ignore_logs_requests
        self.write_delay = write_delay
        self.records = list(records or [])
        self.status = status
        self.stop_after_first_packet = stop_after_first_packet
        self.writes: list[bytes] = []
        self.timeline: list[str] = []
        self._notify = None
        self.disconnected = False
        char = SimpleNamespace(properties=["write", "write-without-response"])
        self.services = SimpleNamespace(get_characteristic=lambda uuid: char)

    async def start_notify(self, char: str, callback) -> None:
        self._notify = callback

    def _reply(self, data: bytes) -> None:
        asyncio.get_running_loop().call_soon(self._notify, None, bytearray(data))

    async def write_gatt_char(self, char: Any, data: bytes, response: bool = True) -> None:
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.writes.append(bytes(data))
        if data == REQUEST_STATUS:
            self.timeline.append("status")
            if self.status:
                self._reply(self.status)
        elif data.startswith(bytes.fromhex("505400140305")):
            self.timeline.append("settings")
            self._reply(bytes.fromhex("5250000F0305") + data[6:8] + bytes(13))
        elif data == REQUEST_WATER_LOGS:
            self.timeline.append("logs")
            if self.ignore_logs_requests > 0:
                self.ignore_logs_requests -= 1
                return
            if not self.records:
                self._reply(bytes([0x52, 0x50, 0, 3, 2, 6, 0]))
                return
            total = len(self.records) * 13
            self._reply(bytes([0x50, 0x54, total >> 8, total & 0xFF, 13, 6]) + self.records[0])
            if not self.stop_after_first_packet:
                for rec in self.records[1:]:
                    self._reply(bytes([13, 6]) + rec)
        elif data == REQUEST_RECALIBRATE_EMPTY:
            self.timeline.append("recalibrate")
            for code in self.recalibrate_codes:
                self._reply(bytes([0x52, 0x54, 0, 3, 2, 0xA1, code]))
        elif data.startswith(bytes.fromhex("525000040306")):
            self.timeline.append("clear")
        elif data.startswith(bytes.fromhex("505400040304")):
            self.timeline.append("total")
            self._reply(bytes.fromhex("52500003020400"))

    async def disconnect(self) -> None:
        self.disconnected = True


def _session(bottle: FakeBottle, *, now: datetime, goal: int | None, update: bool = True,
             today: int | None = 999, fail_store: bool = False):
    saved: list[list[LogRecord]] = []

    async def store(records: list[LogRecord]) -> int | None:
        bottle.timeline.append("store")
        if fail_store:
            raise RuntimeError("disk full")
        saved.append(records)
        return today

    session = BottleSession(
        bottle, now=lambda: now, goal_ml=goal, update_bottle=update,
        store_drinks=store, command_delay=0,
    )
    return session, saved


# ---- session safety rules -------------------------------------------------


async def test_full_sync_saves_before_clearing() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    drinks = [_record(now - timedelta(minutes=m), ml) for m, ml in ((30, 120), (20, 80))]
    flagged = _record(now - timedelta(minutes=10), 0, flag=1)
    bottle = FakeBottle(drinks + [flagged])
    session, saved = _session(bottle, now=now, goal=3000)

    result = await session.run()

    # Goal differs (4347 on the bottle) so clock+goal are set; save, then clear all 3 records.
    assert bottle.timeline == ["status", "settings", "logs", "store", "clear", "total"]
    assert [r.amount_ml for r in saved[0]] == [120, 80]
    assert clear_records(3) in bottle.writes
    assert result.cleared == 3 and result.complete
    assert result.total_written == 999
    assert result.status is not None and result.status.battery == 99


async def test_settings_skipped_when_clock_and_goal_already_right() -> None:
    now = datetime(2026, 11, 17, 23, 0, 10)
    bottle = FakeBottle(status=_status_with_clock(now))
    session, _ = _session(bottle, now=now, goal=4347, today=2575)
    result = await session.run()
    # Nothing stored on the bottle, clock within 30 s, goal and total equal: only reads.
    assert bottle.timeline == ["status", "logs", "store"]
    assert not result.settings_written and result.total_written is None


async def test_failed_save_never_clears() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    bottle = FakeBottle([_record(now, 200)])
    session, _ = _session(bottle, now=now, goal=None, fail_store=True)
    with pytest.raises(RuntimeError):
        await session.run()
    assert "clear" not in bottle.timeline and "total" not in bottle.timeline


async def test_partial_read_saves_what_arrived_but_clears_nothing() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    bottle = FakeBottle([_record(now, 100), _record(now, 50)], stop_after_first_packet=True)
    session, saved = _session(bottle, now=now, goal=None)
    with (
        patch("custom_components.waterh.ble_session.LOGS_NEXT_TIMEOUT_S", 0.05),
        pytest.raises(BottleSyncError, match="nothing was cleared"),
    ):
        await session.run()
    assert [r.amount_ml for r in saved[0]] == [100]
    assert "clear" not in bottle.timeline


async def test_update_bottle_off_only_reads_and_clears() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    bottle = FakeBottle([_record(now, 300)])
    session, _ = _session(bottle, now=now, goal=3000, update=False)
    await session.run()
    assert bottle.timeline == ["status", "logs", "store", "clear"]


async def test_stored_drinks_request_is_retried_once() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    bottle = FakeBottle([_record(now, 150)], ignore_logs_requests=1)
    session, saved = _session(bottle, now=now, goal=None)
    with patch("custom_components.waterh.ble_session.LOGS_FIRST_TIMEOUT_S", 0.05):
        result = await session.run()
    assert bottle.timeline == ["status", "settings", "logs", "logs", "store", "clear", "total"]
    assert [r.amount_ml for r in saved[0]] == [150]
    assert result.cleared == 1


async def test_stored_drinks_never_answered_clears_nothing() -> None:
    now = datetime(2026, 11, 17, 23, 5, 0)
    bottle = FakeBottle([_record(now, 150)], ignore_logs_requests=2)
    session, saved = _session(bottle, now=now, goal=None)
    with (
        patch("custom_components.waterh.ble_session.LOGS_FIRST_TIMEOUT_S", 0.05),
        pytest.raises(BottleSyncError, match="asked twice"),
    ):
        await session.run()
    assert bottle.timeline == ["status", "settings", "logs", "logs"]
    assert saved == []


async def test_recalibrate_succeeds_and_touches_nothing_else() -> None:
    bottle = FakeBottle([_record(datetime(2026, 11, 17, 8), 100)])
    session, saved = _session(bottle, now=datetime(2026, 11, 17, 9), goal=3000)
    status = await session.recalibrate()
    assert status.battery == 99
    # Only status and the recalibrate command: no settings, no drinks read or cleared.
    assert bottle.timeline == ["status", "recalibrate"]
    assert saved == []


async def test_recalibrate_failure_is_reported() -> None:
    bottle = FakeBottle(recalibrate_codes=(2, 4))
    session, _ = _session(bottle, now=datetime(2026, 11, 17, 9), goal=None)
    with pytest.raises(BottleSyncError, match="recalibration failed"):
        await session.recalibrate()


async def test_recalibrate_without_result_times_out() -> None:
    bottle = FakeBottle(recalibrate_codes=(2,))
    session, _ = _session(bottle, now=datetime(2026, 11, 17, 9), goal=None)
    with (
        patch("custom_components.waterh.ble_session.RECALIBRATE_TIMEOUT_S", 0.05),
        pytest.raises(BottleSyncError, match="started recalibrating but"),
    ):
        await session.recalibrate()


async def test_silent_bottle_is_an_error() -> None:
    bottle = FakeBottle(status=None)
    session, _ = _session(bottle, now=datetime(2026, 11, 17), goal=None)
    with (
        patch("custom_components.waterh.ble_session.STATUS_TIMEOUT_S", 0.05),
        pytest.raises(BottleSyncError, match="didn't answer"),
    ):
        await session.run()
    assert bottle.timeline == ["status", "status"]


# ---- Home Assistant side --------------------------------------------------


async def _create_bluetooth_entry(hass: HomeAssistant):
    hass.config.components.add("bluetooth")
    with patch(
        "custom_components.waterh.config_flow._discovered_bottles",
        return_value={ADDRESS: "WaterH-Boost-946ml"},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.MENU
        # Bluetooth is the first (recommended) choice.
        assert result["menu_options"][0] == "pick_bluetooth"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "pick_bluetooth"}
        )
        assert result["step_id"] == "pick_bluetooth"
        assert result["description_placeholders"]["count"] == "1"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": ADDRESS}
        )
        assert result["step_id"] == "bluetooth_confirm"
        with patch("custom_components.waterh.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"name": "Bottle"}
            )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bottle (Bluetooth)"
    assert result["data"]["mode"] == "bluetooth"
    assert result["data"]["address"] == ADDRESS
    assert result["options"] == {"sync_interval": 5, "update_bottle": True, "android_app": False}
    return result["result"]


def _ble_patches(bottle: FakeBottle, present: bool = True):
    device = SimpleNamespace(address=ADDRESS, name="WaterH-Boost-946ml")
    bt = bluetooth_sync.bluetooth
    return [
        patch.object(bt, "async_address_present", return_value=present),
        patch.object(bt, "async_register_callback", return_value=lambda: None),
        patch.object(bt, "async_track_unavailable", return_value=lambda: None),
        patch.object(
            bt, "async_ble_device_from_address", return_value=device if present else None
        ),
        patch.object(bt, "async_last_service_info", return_value=None),
        patch.object(bluetooth_sync, "establish_connection", return_value=bottle),
        patch("custom_components.waterh.ble_session.asyncio.sleep", return_value=None),
    ]


async def test_bluetooth_entry_end_to_end(hass: HomeAssistant) -> None:
    entry = await _create_bluetooth_entry(hass)
    now_local = dt_util.now().replace(tzinfo=None, microsecond=0)
    bottle = FakeBottle([_record(now_local - timedelta(minutes=5), 250)])

    patches = _ble_patches(bottle, present=False)  # out of range at start: no auto sync
    for p in patches:
        p.start()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        assert coordinator.ble is not None
        assert hass.states.get("binary_sensor.waterh_bottle_bottle_in_range").state == "off"
        assert hass.states.get("sensor.waterh_bottle_bluetooth_sync").state == "waiting"
        # No webhook for a Bluetooth-only entry.
        assert entry.data["webhook_id"] not in hass.data.get("webhook", {})
    finally:
        for p in patches:
            p.stop()

    patches = _ble_patches(bottle, present=True)
    for p in patches:
        p.start()
    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.waterh_bottle_sync_bottle_now"},
            blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()

    assert bottle.disconnected
    assert bottle.timeline[-2:] == ["clear", "total"] or bottle.timeline[-1] == "clear"
    assert hass.states.get("sensor.waterh_bottle_bluetooth_sync").state == "ok"
    assert hass.states.get("sensor.waterh_bottle_drinks_today").state == "1"
    assert hass.states.get("sensor.waterh_bottle_bottle_battery").state == "99"
    assert hass.states.get("binary_sensor.waterh_bottle_bottle_charging").state == "on"
    drink = coordinator.data.drinks[0]
    assert drink.water_amount == 250
    assert drink.id.startswith("bottle-")
    # Same id scheme as the Android app, so a drink seen by both is stored once.
    assert drink.created == dt_util.as_utc(
        (now_local - timedelta(minutes=5)).replace(tzinfo=dt_util.get_default_time_zone())
    )


async def test_button_during_a_sync_waits_instead_of_reconnecting(hass: HomeAssistant) -> None:
    entry = await _create_bluetooth_entry(hass)
    bottle = FakeBottle(write_delay=0.05)
    patches = _ble_patches(bottle, present=False)
    for p in patches:
        p.start()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()

    patches = _ble_patches(bottle, present=True)
    started = [p.start() for p in patches]
    connect = started[5]  # establish_connection
    try:
        ble = entry.runtime_data.ble
        running = hass.async_create_task(ble.async_sync())
        await asyncio.sleep(0.01)  # the first sync is now connected and talking
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.waterh_bottle_sync_bottle_now"},
            blocking=True,
        )
        await running
    finally:
        for p in patches:
            p.stop()
    assert connect.call_count == 1
    assert bottle.timeline.count("status") == 1
    assert hass.states.get("sensor.waterh_bottle_bluetooth_sync").state == "ok"


async def test_next_sync_runs_one_interval_after_the_last(
    hass: HomeAssistant, freezer
) -> None:
    """A quiet bottle (no advertisements) is synced one interval after the last
    sync, not on a timer counted from when the integration loaded."""
    entry = await _create_bluetooth_entry(hass)
    bottle = FakeBottle()
    patches = _ble_patches(bottle, present=False)
    for p in patches:
        p.start()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        for p in patches:
            p.stop()

    patches = _ble_patches(bottle, present=True)
    started = [p.start() for p in patches]
    connect = started[5]
    try:
        ble = entry.runtime_data.ble
        ble.in_range = True
        # A sync 2 minutes after loading (e.g. Sync bottle now).
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass)
        assert await ble.async_sync()
        assert connect.call_count == 1
        # 4 minutes after that sync: not due (a timer counted from loading
        # would fire here and skip, leaving the next sync ~10 min away).
        freezer.tick(timedelta(minutes=4))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.call_count == 1
        # Just over 5 minutes after the sync: due, no advertisement needed.
        freezer.tick(timedelta(minutes=1, seconds=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert connect.call_count == 2
    finally:
        for p in patches:
            p.stop()


async def test_recalibrate_button(hass: HomeAssistant) -> None:
    entry = await _create_bluetooth_entry(hass)
    bottle = FakeBottle()
    patches = _ble_patches(bottle, present=False)
    for p in patches:
        p.start()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()

    patches = _ble_patches(bottle, present=True)
    patches.append(
        patch("custom_components.waterh.button.persistent_notification.async_create")
    )
    started = [p.start() for p in patches]
    notify = started[-1]
    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.waterh_bottle_recalibrate_bottle"},
            blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()
    assert bottle.timeline == ["status", "recalibrate"]
    assert bottle.disconnected
    attrs = hass.states.get("sensor.waterh_bottle_bluetooth_sync").attributes
    assert attrs["last_recalibration_result"] == "ok"
    notify.assert_called_once()
    assert "recalibrated" in notify.call_args.args[1]

    # A failed recalibration shows an error and records why.
    bottle = FakeBottle(recalibrate_codes=(2, 4))
    patches = _ble_patches(bottle, present=True)
    for p in patches:
        p.start()
    try:
        from homeassistant.exceptions import HomeAssistantError

        with pytest.raises(HomeAssistantError, match="recalibration failed"):
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.waterh_bottle_recalibrate_bottle"},
                blocking=True,
            )
    finally:
        for p in patches:
            p.stop()
    attrs = hass.states.get("sensor.waterh_bottle_bluetooth_sync").attributes
    assert "recalibration failed" in attrs["last_recalibration_result"]


async def test_failed_sync_is_reported(hass: HomeAssistant) -> None:
    entry = await _create_bluetooth_entry(hass)
    bottle = FakeBottle(status=None)
    patches = _ble_patches(bottle, present=False)
    for p in patches:
        p.start()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()

    patches = _ble_patches(bottle, present=True)
    patches.append(patch("custom_components.waterh.ble_session.STATUS_TIMEOUT_S", 0.01))
    for p in patches:
        p.start()
    try:
        from homeassistant.exceptions import HomeAssistantError

        with pytest.raises(HomeAssistantError, match="didn't answer"):
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.waterh_bottle_sync_bottle_now"},
                blocking=True,
            )
    finally:
        for p in patches:
            p.stop()
    state = hass.states.get("sensor.waterh_bottle_bluetooth_sync")
    assert state.state == "failed"
    assert "didn't answer" in state.attributes["last_error"]


async def test_options_need_a_source_and_can_add_the_app(hass: HomeAssistant) -> None:
    entry = await _create_bluetooth_entry(hass)
    with (
        patch("custom_components.waterh.async_setup_entry", return_value=True),
        patch("custom_components.waterh.config_flow._discovered_bottles", return_value={}),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "sync"
        base = {"sync_interval": 10, "update_bottle": True, "volume_unit": "auto"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**base, "address": "off", "android_app": False}
        )
        assert result["errors"] == {"base": "no_source"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**base, "address": "nonsense", "android_app": False}
        )
        assert result["errors"] == {"address": "invalid_address"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**base, "address": ADDRESS, "android_app": True}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["android_app"] is True
    assert entry.options["sync_interval"] == 10


async def test_discovery_offers_the_bottle_once(hass: HomeAssistant) -> None:
    hass.config.components.add("bluetooth")
    info = SimpleNamespace(address=ADDRESS.lower(), name="WaterH-Boost-946ml")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=info
    )
    assert result["step_id"] == "bluetooth_confirm"
    with patch("custom_components.waterh.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Bottle"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=info
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"


async def test_pick_bluetooth_without_bluetooth_explains(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pick_bluetooth"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_bluetooth"
