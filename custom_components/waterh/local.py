"""Local mode: drinks stored in Home Assistant instead of WaterH's cloud.

The WaterH Sync Android app reads drinks off the bottle over Bluetooth and
posts them to the integration's webhook. They're kept here, on disk, and
saved before the app is told it may clear them from the bottle.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import WaterHDrink
from .const import DOMAIN, LOCAL_DEFAULT_GOAL_ML, LOCAL_KEEP_DAYS, SOURCE_MANUAL

STORAGE_VERSION = 1


def bottle_drink_id(created: datetime, amount_ml: int) -> str:
    """Stable id for a bottle record, so a re-sent report isn't counted twice."""
    return f"bottle-{int(created.timestamp())}-{amount_ml}"


def manual_drink_id() -> str:
    return f"manual-{uuid.uuid4().hex[:12]}"


class LocalDrinkStore:
    """Drinks and goal for a local-mode entry, persisted in .storage."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.drinks: list[WaterHDrink] = []
        self.goal_ml: int = LOCAL_DEFAULT_GOAL_ML
        self.last_report: datetime | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.goal_ml = int(data.get("goal_ml") or LOCAL_DEFAULT_GOAL_ML)
        if last := data.get("last_report"):
            self.last_report = dt_util.parse_datetime(last)
        self.drinks = []
        for item in data.get("drinks", []):
            created = dt_util.parse_datetime(item["created"])
            if created is None:
                continue
            self.drinks.append(
                WaterHDrink(
                    id=item["id"],
                    water_amount=int(item["water_amount"]),
                    amount=int(item["amount"]),
                    created=created,
                    source_id=item.get("source_id"),
                    mac_address=None,
                    water_type=item.get("water_type"),
                    temperature=item.get("temperature"),
                    tds=item.get("tds"),
                )
            )
        self.drinks.sort(key=lambda d: d.created)

    async def async_save(self) -> None:
        """Write to disk now (not delayed): the app clears the bottle once we reply."""
        self._prune()
        await self._store.async_save(
            {
                "goal_ml": self.goal_ml,
                "last_report": self.last_report.isoformat() if self.last_report else None,
                "drinks": [
                    {
                        "id": d.id,
                        "water_amount": d.water_amount,
                        "amount": d.amount,
                        "created": d.created.isoformat(),
                        "source_id": d.source_id,
                        "water_type": d.water_type,
                        "temperature": d.temperature,
                        "tds": d.tds,
                    }
                    for d in self.drinks
                ],
            }
        )

    def add(self, drinks: list[WaterHDrink]) -> int:
        """Add drinks, skipping ids already stored. Returns how many were new."""
        known = {d.id for d in self.drinks}
        new = [d for d in drinks if d.id not in known]
        self.drinks.extend(new)
        self.drinks.sort(key=lambda d: d.created)
        return len(new)

    def remove(self, drink_id: str) -> bool:
        before = len(self.drinks)
        self.drinks = [d for d in self.drinks if d.id != drink_id]
        return len(self.drinks) != before

    def manual_drink(self, amount_ml: int, water_type: str, when: datetime | None) -> WaterHDrink:
        return WaterHDrink(
            id=manual_drink_id(),
            water_amount=amount_ml,
            amount=amount_ml,
            created=dt_util.as_utc(when) if when else dt_util.utcnow(),
            source_id=SOURCE_MANUAL,
            mac_address=None,
            water_type=water_type,
            temperature=None,
            tds=None,
        )

    def drinks_between(self, start: datetime, end: datetime) -> list[WaterHDrink]:
        return [d for d in self.drinks if start <= d.created < end]

    def _prune(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=LOCAL_KEEP_DAYS)
        self.drinks = [d for d in self.drinks if d.created >= cutoff]
