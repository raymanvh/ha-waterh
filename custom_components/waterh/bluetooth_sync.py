"""Home Assistant syncs the bottle itself, over Bluetooth.

Home Assistant connects to the bottle through any Bluetooth adapter or
ESPHome Bluetooth proxy that can hear it. Whenever the bottle is in range and
the sync interval has passed since the last attempt, one session runs (see
ble_session.py): drinks stored on the bottle are saved in Home Assistant,
then cleared from the bottle, and the bottle's clock, goal and today's total
are set to match Home Assistant.

Drinks taken away from home stay on the bottle (with their times) until the
next sync, so nothing is lost, only delayed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .ble_session import BottleSession, BottleSyncError, SessionResult
from .const import (
    SYNC_STATUS_FAILED,
    SYNC_STATUS_NOT_IN_RANGE,
    SYNC_STATUS_OK,
    SYNC_STATUS_SYNCING,
    SYNC_STATUS_WAITING,
)

if TYPE_CHECKING:
    from .coordinator import WaterHConfigEntry, WaterHCoordinator

_LOGGER = logging.getLogger(__name__)

# Whole-session limit, connection included.
SESSION_TIMEOUT_S = 90
# A timer firing a moment before the interval is up still counts as due.
DUE_SLACK = timedelta(seconds=15)

STATUS_WAITING = SYNC_STATUS_WAITING
STATUS_SYNCING = SYNC_STATUS_SYNCING
STATUS_OK = SYNC_STATUS_OK
STATUS_NOT_IN_RANGE = SYNC_STATUS_NOT_IN_RANGE
STATUS_FAILED = SYNC_STATUS_FAILED


class BottleBluetooth:
    """Watches for the bottle and syncs it on a schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WaterHConfigEntry,
        coordinator: WaterHCoordinator,
        address: str,
        interval: timedelta,
        update_bottle: bool,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.address = address.upper()
        self.interval = interval
        self.update_bottle = update_bottle

        self.status = STATUS_WAITING
        self.in_range = False
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.last_result: SessionResult | None = None
        self.via: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- watching -------------------------------------------------------

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start watching; returns a function that stops it."""
        self.in_range = bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )
        unsubs = [
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
                bluetooth.BluetoothScanningMode.PASSIVE,
            ),
            bluetooth.async_track_unavailable(
                self.hass, self._on_unavailable, self.address, connectable=True
            ),
            async_track_time_interval(self.hass, self._on_timer, self.interval),
        ]
        if self.in_range:
            self.async_schedule()

        @callback
        def _stop() -> None:
            for unsub in unsubs:
                unsub()
            if self._task and not self._task.done():
                self._task.cancel()

        return _stop

    @callback
    def _on_advertisement(
        self, info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        if not self.in_range:
            self.in_range = True
            self.coordinator.async_update_listeners()
        self.async_schedule()

    @callback
    def _on_unavailable(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        self.in_range = False
        self.coordinator.async_update_listeners()

    @callback
    def _on_timer(self, _now: datetime) -> None:
        if self.in_range:
            self.async_schedule()

    @property
    def next_due(self) -> datetime | None:
        if self.last_attempt is None:
            return None
        return self.last_attempt + self.interval

    @callback
    def async_schedule(self, force: bool = False) -> None:
        """Start a sync in the background if one is due (or [force])."""
        if self._task is not None and not self._task.done():
            return
        if (
            not force
            and self.last_attempt is not None
            and dt_util.utcnow() - self.last_attempt < self.interval - DUE_SLACK
        ):
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self.async_sync(), f"WaterH Bluetooth sync {self.address}"
        )

    # ---- syncing --------------------------------------------------------

    async def async_sync_now(self) -> bool:
        """Sync now for the button. If a sync is already running, wait for it instead.

        Back-to-back connections give the bottle no time to settle, and the
        running sync already does what the button asks.
        """
        if self._lock.locked():
            async with self._lock:
                pass
            return self.status == STATUS_OK
        return await self.async_sync()

    async def async_sync(self) -> bool:
        """Run one sync now. Returns True on success; never raises."""
        async with self._lock:
            self.last_attempt = dt_util.utcnow()
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                self.in_range = False
                self._finish(
                    STATUS_NOT_IN_RANGE,
                    "The bottle isn't in range of a Bluetooth adapter or proxy that "
                    "can connect. Tilt or pick it up to wake it.",
                )
                return False
            self.in_range = True
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            self.via = self._scanner_name(info.source) if info else None
            self.status = STATUS_SYNCING
            self.coordinator.async_update_listeners()

            try:
                async with asyncio.timeout(SESSION_TIMEOUT_S):
                    result = await self._connect_and_run(ble_device)
            except BleakOutOfConnectionSlotsError:
                self._finish(
                    STATUS_FAILED,
                    "Every Bluetooth adapter/proxy in range is busy (no free connection "
                    "slots). Will try again next interval.",
                )
                return False
            except BleakNotFoundError:
                self.in_range = False
                self._finish(STATUS_NOT_IN_RANGE, "The bottle went out of range.")
                return False
            except BottleSyncError as err:
                self._finish(STATUS_FAILED, str(err))
                return False
            except TimeoutError:
                self._finish(STATUS_FAILED, "The sync took too long and was stopped.")
                return False
            except Exception as err:  # Bleak/proxy errors come in many types.
                _LOGGER.debug("WaterH Bluetooth sync failed", exc_info=True)
                self._finish(
                    STATUS_FAILED,
                    f"Couldn't connect to the bottle: {err or type(err).__name__}. "
                    "It may be connected to a phone (only one connection at a time).",
                )
                return False

            self.last_result = result
            self.last_success = dt_util.utcnow()
            self._finish(STATUS_OK, None)
            return True

    async def _connect_and_run(self, ble_device) -> SessionResult:
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or "WaterH bottle",
            max_attempts=3,
        )
        try:
            session = BottleSession(
                client,
                now=lambda: dt_util.now().replace(tzinfo=None),
                goal_ml=self.coordinator.data.goal_ml if self.coordinator.data else None,
                update_bottle=self.update_bottle,
                store_drinks=lambda records: self.coordinator.async_receive_bluetooth_records(
                    records, (ble_device.name or "waterh-bottle").lower()
                ),
            )
            try:
                return await session.run()
            finally:
                self.last_result = session.result
                if session.result.status is not None:
                    self.coordinator.async_set_bottle_from_status(
                        session.result.status, session.result.total_written
                    )
        finally:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug("Disconnect from WaterH bottle failed", exc_info=True)

    def _scanner_name(self, source: str) -> str:
        scanner = bluetooth.async_scanner_by_source(self.hass, source)
        return scanner.name if scanner else source

    @callback
    def _finish(self, status: str, error: str | None) -> None:
        self.status = status
        self.last_error = error
        if error:
            _LOGGER.debug("WaterH Bluetooth sync: %s", error)
        self.coordinator.async_update_listeners()
