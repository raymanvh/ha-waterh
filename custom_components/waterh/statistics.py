"""Water intake as long-term statistics, at the times the drinks were taken.

Sensor states are recorded when Home Assistant hears about a change, so
drinks that sync late (a bottle that was away from home, an app opened
hours later) show up in history as one jump at sync time. Each drink
carries its own time, though, so this keeps an hourly external statistic,
``waterh:water_intake_<entry>``, built from those times and rewritten
whenever a late drink fills in an earlier hour. Use it in a Statistics
Graph card (or the Energy-style "change" view) to see when you drank.

Statistics are hourly; Home Assistant has no finer long-term resolution.

How it stays consistent across restarts and pruning: drinks are summed
into hourly buckets (persisted here). Buckets inside the source's window
(local mode: the stored drinks; cloud mode: today) are recomputed from the
drinks each time; older buckets are kept as they were. Buckets older than
KEEP roll into ``base_ml``, the running total before ``base_hour``, so the
cumulative sum never moves for hours already written.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_change_statistics_unit,
    get_metadata,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN, LOCAL_KEEP_DAYS, ML_PER_FL_OZ, UNIT_OZ
from .units import entry_unit, ha_unit

if TYPE_CHECKING:
    from .api import WaterHDrink
    from .coordinator import WaterHConfigEntry, WaterHCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
HOUR = 3600
# Hours rewritten on every change. Longer than local mode keeps drinks, so a
# bucket is only frozen into the base after its drinks are gone anyway.
KEEP = timedelta(days=LOCAL_KEEP_DAYS + 2)


def intake_statistic_id(entry: WaterHConfigEntry) -> str:
    return f"{DOMAIN}:water_intake_{slugify(entry.entry_id)}"


def _floor_hour(ts: float) -> int:
    return int(ts) - int(ts) % HOUR


def _ceil_hour(ts: float) -> int:
    floor = _floor_hour(ts)
    return floor if floor == ts else floor + HOUR


class IntakeStatistics:
    """Keeps one entry's hourly intake statistic in step with its drinks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WaterHConfigEntry,
        coordinator: WaterHCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.statistic_id = intake_statistic_id(entry)
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.statistics"
        )
        # Running total (mL) of everything before base_hour.
        self._base_ml = 0
        self._base_hour: int | None = None
        # Hour start (epoch seconds, UTC) -> mL drunk in that hour, for hours >= base_hour.
        self._buckets: dict[int, int] = {}
        # Whether the zero row before base_hour has been written.
        self._seeded = False
        self._signature: tuple | None = None
        self._unit_checked: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._again = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._base_ml = int(data.get("base_ml", 0))
        self._base_hour = data.get("base_hour")
        self._buckets = {int(h): int(ml) for h, ml in data.get("buckets", {}).items()}
        self._seeded = bool(data.get("seeded"))

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def async_schedule(self) -> None:
        """Coordinator listener: recompute soon (coalesces bursts of updates)."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} intake statistics"
        )

    async def _async_run(self) -> None:
        while True:
            self._again = False
            try:
                await self.async_update()
            except Exception:  # Never break the integration over a chart.
                _LOGGER.exception("Couldn't update the water intake statistics")
            if not self._again:
                return

    async def async_update(self, now: datetime | None = None) -> None:
        """Recompute from the coordinator's drinks and write if anything changed."""
        now = now or dt_util.utcnow()
        coordinator = self.coordinator
        if coordinator.store is not None:
            drinks: Iterable[WaterHDrink] = list(coordinator.store.drinks)
            window_start = now - timedelta(days=LOCAL_KEEP_DAYS)
        elif coordinator.data is not None and coordinator.data.day_start is not None:
            drinks = list(coordinator.data.drinks)
            window_start = coordinator.data.day_start
        else:
            return
        unit = entry_unit(self.hass, self.entry)
        async with self._lock:
            await self._async_apply(drinks, window_start, now, unit)

    async def _async_apply(
        self,
        drinks: Iterable[WaterHDrink],
        window_start: datetime,
        now: datetime,
        unit: str,
    ) -> None:
        now_hour = _floor_hour(now.timestamp())
        # Partly pruned hours stay as they were: start at the first whole hour.
        window_hour = _ceil_hour(window_start.timestamp())
        if self._base_hour is None:
            self._base_hour = min(window_hour, now_hour)
        window_hour = max(window_hour, self._base_hour)

        buckets = {h: ml for h, ml in self._buckets.items() if h < window_hour}
        for drink in drinks:
            # A bottle clock running ahead can't put drinks in hours that haven't happened.
            hour = min(_floor_hour(drink.created.timestamp()), now_hour)
            if hour < window_hour:
                continue
            buckets[hour] = buckets.get(hour, 0) + drink.water_amount
        buckets = {h: ml for h, ml in buckets.items() if ml}

        base_ml, base_hour = self._base_ml, self._base_hour
        frozen_before = _floor_hour((now - KEEP).timestamp())
        if frozen_before > base_hour:
            for hour in [h for h in buckets if h < frozen_before]:
                base_ml += buckets.pop(hour)
            base_hour = frozen_before

        signature = (base_ml, base_hour, tuple(sorted(buckets.items())), now_hour, unit)
        if signature == self._signature:
            return

        self._base_ml, self._base_hour, self._buckets = base_ml, base_hour, buckets
        await self._async_write(now_hour, unit)
        self._seeded = True
        self._signature = signature
        await self._store.async_save(
            {
                "base_ml": self._base_ml,
                "base_hour": self._base_hour,
                "buckets": {str(h): ml for h, ml in sorted(self._buckets.items())},
                "seeded": self._seeded,
            }
        )

    async def _async_write(self, now_hour: int, unit: str) -> None:
        unit_of_measurement = ha_unit(unit)
        await self._async_match_unit(unit_of_measurement)
        factor = 1 / ML_PER_FL_OZ if unit == UNIT_OZ else 1.0
        assert self._base_hour is not None

        # The first time, add a row before the first hour so its intake counts
        # as a change (a statistic's first row has nothing to compare with).
        start = self._base_hour if self._seeded else self._base_hour - HOUR
        total = self._base_ml
        day_total = 0
        day: datetime | None = None
        rows: list[StatisticData] = []
        for hour in range(start, now_hour + 1, HOUR):
            start_utc = datetime.fromtimestamp(hour, UTC)
            day_start = dt_util.start_of_local_day(dt_util.as_local(start_utc))
            if day_start != day:
                day, day_total = day_start, 0
            ml = self._buckets.get(hour, 0)
            total += ml
            day_total += ml
            rows.append(
                StatisticData(
                    start=start_utc,
                    # Total since tracking started; a Statistics Graph "change"
                    # shows the intake per hour/day/week from it.
                    sum=total * factor,
                    # Today's running total at the end of this hour.
                    state=day_total * factor,
                    last_reset=dt_util.as_utc(day_start),
                )
            )

        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                source=DOMAIN,
                statistic_id=self.statistic_id,
                name=f"{self.entry.title} water intake",
                unit_of_measurement=unit_of_measurement,
                unit_class=VolumeConverter.UNIT_CLASS,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
            ),
            rows,
        )

    async def _async_match_unit(self, unit_of_measurement: str) -> None:
        """Convert rows already stored when the display unit changes (mL <-> fl. oz.).

        Hours older than KEEP aren't rewritten, so they're converted in place
        rather than left in the old unit. Queued before the new rows, and the
        recorder runs its queue in order.
        """
        if self._unit_checked == unit_of_measurement:
            return
        metadata = await get_instance(self.hass).async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids={self.statistic_id})
        )
        if self.statistic_id in metadata:
            old = metadata[self.statistic_id][1]["unit_of_measurement"]
            if old and old != unit_of_measurement:
                await async_change_statistics_unit(
                    self.hass,
                    self.statistic_id,
                    new_unit_of_measurement=unit_of_measurement,
                    old_unit_of_measurement=old,
                )
        self._unit_checked = unit_of_measurement
