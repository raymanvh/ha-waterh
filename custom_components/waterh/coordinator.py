"""Data update coordinator for WaterH."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    WaterHApiClient,
    WaterHAuthError,
    WaterHDrink,
    WaterHError,
    WaterHRateLimitError,
)
from .const import (
    CONF_APP_REPORTED,
    CONF_SCAN_INTERVAL,
    CONF_USER_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SOURCE_MANUAL,
)
from .local import LocalDrinkStore, bottle_drink_id
from .protocol import BoostStatus, LogRecord, ReminderSettings

if TYPE_CHECKING:
    from .bluetooth_sync import BottleBluetooth

_LOGGER = logging.getLogger(__name__)


@dataclass
class WaterHData:
    """Snapshot of today's hydration."""

    drinks: list[WaterHDrink] = field(default_factory=list)
    goal_ml: int | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    day_start: datetime | None = None
    fetched_at: datetime | None = None

    @property
    def total_ml(self) -> int:
        return sum(d.water_amount for d in self.drinks)

    @property
    def liquid_total_ml(self) -> int:
        return sum(d.amount for d in self.drinks)

    @property
    def bottle_total_ml(self) -> int:
        return sum(d.water_amount for d in self.drinks if d.source_id != SOURCE_MANUAL)

    @property
    def manual_total_ml(self) -> int:
        return sum(d.water_amount for d in self.drinks if d.source_id == SOURCE_MANUAL)

    @property
    def last_drink(self) -> WaterHDrink | None:
        return self.drinks[-1] if self.drinks else None

    @property
    def last_bottle_drink(self) -> WaterHDrink | None:
        for drink in reversed(self.drinks):
            if drink.source_id != SOURCE_MANUAL:
                return drink
        return None

    @property
    def goal_progress(self) -> float | None:
        if not self.goal_ml:
            return None
        return round(self.total_ml / self.goal_ml * 100, 1)


@dataclass
class BottleStatus:
    """Bottle state read over Bluetooth, by Home Assistant or the Android app (not in the cloud)."""

    battery: int
    charging: bool | None
    firmware: str | None
    today_ml: int | None
    reported_at: datetime


type WaterHConfigEntry = ConfigEntry[WaterHCoordinator]


class WaterHCoordinator(DataUpdateCoordinator[WaterHData]):
    """Today's drinks and goal, from WaterH's cloud or (local mode) from Home Assistant.

    Cloud mode polls WaterH. Local mode has no polling source: drinks arrive
    from the WaterH Sync app through the webhook and live in [store]; the
    1-minute interval only rolls the totals over at midnight.
    """

    config_entry: WaterHConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WaterHConfigEntry,
        client: WaterHApiClient | None,
        store: LocalDrinkStore | None = None,
    ) -> None:
        if store is not None:
            interval = timedelta(minutes=1)
        else:
            minutes = entry.options.get(CONF_SCAN_INTERVAL)
            interval = (
                timedelta(minutes=minutes) if minutes else DEFAULT_SCAN_INTERVAL
            )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self.client = client
        self.store = store
        # Cloud: the WaterH user id. Local: the entry id (keeps unique ids distinct).
        self.user_id: int | str = (
            f"local_{entry.entry_id}" if store is not None else entry.data[CONF_USER_ID]
        )
        # Settings for the "Log drink" button (kept in Home Assistant only).
        self.quick_amount_ml: int = 250
        self.quick_drink_type: str = "water"
        # Read from the bottle (Home Assistant Bluetooth or the Android app); None until then.
        self.bottle: BottleStatus | None = None
        # Home Assistant's own Bluetooth sync, when this entry uses it.
        self.ble: BottleBluetooth | None = None
        # Reminder (hex) sent to the bottle and not yet read back from it.
        self._reminder_sent: str | None = None
        self._first_report_callbacks: list[Callable[[], None]] = []

    @callback
    def async_add_first_report_callback(self, cb: Callable[[], None]) -> None:
        """Run [cb] the first time the app reports (to create bottle entities)."""
        self._first_report_callbacks.append(cb)

    @property
    def reminder(self) -> ReminderSettings | None:
        """The bottle's reminder settings: as chosen in Home Assistant, else as last read."""
        if self.store is None:
            return None
        return ReminderSettings.from_hex(
            self.store.reminder_hex or self.store.bottle_reminder_hex
        )

    @property
    def reminder_pending(self) -> bool:
        """A reminder change made in Home Assistant hasn't reached the bottle yet."""
        return bool(
            self.store
            and self.store.reminder_hex
            and self.store.reminder_hex != self.store.bottle_reminder_hex
        )

    async def async_set_reminder(self, **changes: int) -> None:
        """Change reminder settings; written to the bottle on the next Bluetooth sync."""
        current = self.reminder
        if self.store is None or current is None:
            raise WaterHError(
                "The bottle's reminder settings aren't known yet. Let it sync once over "
                "Bluetooth, then try again."
            )
        wanted = dataclasses.replace(current, **changes)
        try:
            self.store.reminder_hex = wanted.to_hex()
        except ValueError as err:
            raise WaterHError(str(err)) from err
        await self.store.async_save()
        self.async_update_listeners()
        self._push_to_bottle_soon()

    @callback
    def async_set_bottle_from_status(
        self,
        status: BoostStatus,
        total_written: int | None,
        reminder_written: bool = False,
    ) -> str | None:
        """Bluetooth sync read the bottle's status (and maybe wrote a new reminder).

        A reminder change counts as done only once a later sync reads it back
        from the bottle. If the bottle still doesn't have it on the sync after
        it was written, it's treated as rejected: Home Assistant stops sending
        it and goes back to showing the bottle's own settings. Returns a note
        for the sync log when that happens.
        """
        note: str | None = None
        store = self.store
        if store is not None and status.reminder_hex:
            changed = False
            if store.bottle_reminder_hex != status.reminder_hex:
                store.bottle_reminder_hex = status.reminder_hex
                changed = True
            wanted = store.reminder_hex
            if wanted and status.reminder_hex == wanted:
                self._reminder_sent = None
            elif wanted and self._reminder_sent == wanted:
                # Sent last sync, and the bottle still has something else.
                rejected = ReminderSettings.from_hex(wanted)
                store.reminder_hex = None
                self._reminder_sent = None
                changed = True
                note = (
                    "the bottle didn't accept the reminder "
                    f"({rejected.describe() if rejected else wanted}); showing its own "
                    "settings again"
                )
            elif wanted and reminder_written:
                self._reminder_sent = wanted
            if changed:
                self.hass.async_create_task(store.async_save())
        self.async_set_bottle(
            BottleStatus(
                battery=status.battery,
                charging=status.charging,
                firmware=status.firmware,
                today_ml=total_written if total_written is not None else status.today_ml,
                reported_at=dt_util.utcnow(),
            )
        )
        return note
    @callback
    def async_set_bottle(self, status: BottleStatus) -> None:
        """Store a bottle report (Bluetooth sync or the app) and refresh entities."""
        self.bottle = status
        entry = self.config_entry
        if not entry.data.get(CONF_APP_REPORTED):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_APP_REPORTED: True}
            )
            callbacks, self._first_report_callbacks = self._first_report_callbacks, []
            for cb in callbacks:
                cb()
        self.async_update_listeners()

    @property
    def is_local(self) -> bool:
        return self.store is not None

    # ---- Actions shared by the button, goal number and services ----------
    # Cloud errors surface as WaterHError for the callers to translate.

    async def async_add_drink(
        self, amount_ml: int, water_type: str, when: datetime | None = None
    ) -> None:
        if self.store is not None:
            self.store.add([self.store.manual_drink(amount_ml, water_type, when)])
            await self.store.async_save()
            self.async_set_updated_data(self._local_data())
            self._push_to_bottle_soon()
            return
        assert self.client is not None
        await self.client.async_add_drink(self.user_id, amount_ml, water_type, when)
        await self.async_request_refresh()

    async def async_delete_drink(self, drink_id: str) -> None:
        if self.store is not None:
            if not self.store.remove(drink_id):
                raise WaterHError(f"No drink with id {drink_id}")
            await self.store.async_save()
            self.async_set_updated_data(self._local_data())
            self._push_to_bottle_soon()
            return
        assert self.client is not None
        await self.client.async_delete_drink(drink_id)
        await self.async_request_refresh()

    async def async_set_goal(self, goal_ml: int) -> None:
        if self.store is not None:
            self.store.goal_ml = goal_ml
            await self.store.async_save()
            self.async_set_updated_data(self._local_data())
            self._push_to_bottle_soon()
            return
        assert self.client is not None
        await self.client.async_set_goal(goal_ml)
        await self.async_request_refresh()

    async def async_receive_bottle_drinks(self, drinks: list[WaterHDrink]) -> int:
        """Local mode: store drinks from the app (saved before returning). Returns new count."""
        assert self.store is not None
        added = self.store.add(drinks)
        self.store.last_report = dt_util.utcnow()
        await self.store.async_save()
        self.async_set_updated_data(self._local_data())
        return added

    async def async_receive_bluetooth_records(
        self, records: list[LogRecord], source: str
    ) -> int:
        """Bluetooth sync: save drinks read off the bottle. Returns today's total (mL).

        Times come from the bottle's clock, which is local time (Home
        Assistant's time zone). Ids match the Android app's, so a drink both
        of them see is only stored once.
        """
        assert self.store is not None
        tz = dt_util.get_default_time_zone()
        drinks: list[WaterHDrink] = []
        for record in records:
            naive = record.time.to_naive()
            # A bottle whose clock was never set reports impossible dates; keep
            # the drink, stamped with the sync time, rather than lose it.
            created = (
                dt_util.as_utc(naive.replace(tzinfo=tz)) if naive else dt_util.utcnow()
            )
            drinks.append(
                WaterHDrink(
                    id=bottle_drink_id(created, record.amount_ml),
                    water_amount=record.amount_ml,
                    amount=record.amount_ml,
                    created=created,
                    source_id=source,
                    mac_address=None,
                    water_type="water",
                    # Same scaling the official app uploads (and the Android app sends).
                    temperature=float(record.temperature_raw * 10),
                    tds=record.tds,
                )
            )
        await self.async_receive_bottle_drinks(drinks)
        return self.data.total_ml if self.data else sum(d.water_amount for d in drinks)

    @callback
    def _push_to_bottle_soon(self) -> None:
        """A local change (goal, manual drink): update the bottle now if it's in range."""
        if self.ble is not None and self.ble.in_range:
            self.ble.async_schedule(force=True)

    async def async_note_app_contact(self) -> None:
        """Local mode: record that the app checked in (drives the Last update sensor)."""
        assert self.store is not None
        self.store.last_report = dt_util.utcnow()
        await self.store.async_save()
        self.async_set_updated_data(self._local_data())

    def _local_data(self) -> WaterHData:
        assert self.store is not None
        day_start = dt_util.start_of_local_day(dt_util.now())
        day_end = day_start + timedelta(days=1)
        return WaterHData(
            drinks=self.store.drinks_between(day_start, day_end),
            goal_ml=self.store.goal_ml,
            profile={"first_name": self.config_entry.data.get("name") or "Bottle"},
            day_start=day_start,
            fetched_at=self.store.last_report,
        )

    async def _async_update_data(self) -> WaterHData:
        if self.store is not None:
            return self._local_data()
        assert self.client is not None
        now = dt_util.now()
        day_start = dt_util.start_of_local_day(now)
        day_end = day_start + timedelta(days=1)
        try:
            profile = await self.client.async_get_self()
            drinks = await self.client.async_get_log(self.user_id, day_start, day_end)
        except WaterHAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WaterHRateLimitError as err:
            raise UpdateFailed("Rate limited by WaterH; will retry") from err
        except WaterHError as err:
            raise UpdateFailed(str(err)) from err

        # The API filters by date_finished; keep only today's entries in local time
        # in case the server returns neighbouring records.
        drinks = [d for d in drinks if day_start <= d.created < day_end]

        goal = profile.get("goal")
        return WaterHData(
            drinks=drinks,
            goal_ml=int(goal) if isinstance(goal, (int, float)) and goal > 0 else None,
            profile=profile,
            day_start=day_start,
            fetched_at=dt_util.utcnow(),
        )
