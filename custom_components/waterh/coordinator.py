"""Data update coordinator for WaterH."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

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
    """Bottle state reported by the WaterH Sync Android app (not available from the cloud)."""

    battery: int
    charging: bool | None
    firmware: str | None
    today_ml: int | None
    reported_at: datetime


type WaterHConfigEntry = ConfigEntry[WaterHCoordinator]


class WaterHCoordinator(DataUpdateCoordinator[WaterHData]):
    """Polls today's drink log and the user profile."""

    config_entry: WaterHConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: WaterHConfigEntry, client: WaterHApiClient
    ) -> None:
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
        self.user_id: int = entry.data[CONF_USER_ID]
        # Settings for the "Log drink" button (kept in Home Assistant only).
        self.quick_amount_ml: int = 250
        self.quick_drink_type: str = "water"
        # Pushed by the Android app through the webhook; None until it reports.
        self.bottle: BottleStatus | None = None
        self._first_report_callbacks: list[Callable[[], None]] = []

    @callback
    def async_add_first_report_callback(self, cb: Callable[[], None]) -> None:
        """Run [cb] the first time the app reports (to create bottle entities)."""
        self._first_report_callbacks.append(cb)

    @callback
    def async_set_bottle(self, status: BottleStatus) -> None:
        """Store a report from the app and refresh entities."""
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

    async def _async_update_data(self) -> WaterHData:
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
