"""Time entities for WaterH: when the bottle's drink reminders start and stop each day."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import WaterHReminderEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Reminder start and end times (Bluetooth through Home Assistant only)."""
    coordinator = entry.runtime_data
    if coordinator.ble is None:
        return
    async_add_entities(
        [
            WaterHReminderTime(coordinator, "reminder_start", "start"),
            WaterHReminderTime(coordinator, "reminder_end", "end"),
        ]
    )


class WaterHReminderTime(WaterHReminderEntity, TimeEntity):
    """Start or end of the hours the bottle reminds you in. The end may be after midnight."""

    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: WaterHCoordinator, key: str, which: str) -> None:
        super().__init__(coordinator, key)
        self._which = which

    @property
    def native_value(self) -> time | None:
        reminder = self.coordinator.reminder
        if reminder is None:
            return None
        if self._which == "start":
            return time(reminder.start_hour, reminder.start_minute)
        return time(reminder.end_hour, reminder.end_minute)

    async def async_set_value(self, value: time) -> None:
        await self._async_change(
            **{f"{self._which}_hour": value.hour, f"{self._which}_minute": value.minute}
        )
