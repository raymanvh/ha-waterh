"""Daily goal number entity for WaterH."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WaterHError
from .const import DOMAIN, GOAL_MAX_ML, GOAL_MIN_ML, UNIT_OZ
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import waterh_device_info
from .units import entry_unit, from_ml, ha_unit, to_ml


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the daily goal number."""
    async_add_entities(
        [WaterHGoalNumber(entry.runtime_data, entry_unit(hass, entry))]
    )


class WaterHGoalNumber(CoordinatorEntity[WaterHCoordinator], NumberEntity):
    """Daily hydration goal, editable from Home Assistant."""

    _attr_has_entity_name = True
    _attr_translation_key = "daily_goal"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cup-water"

    def __init__(self, coordinator: WaterHCoordinator, unit: str) -> None:
        super().__init__(coordinator)
        self._unit = unit
        self._attr_unique_id = f"{coordinator.user_id}_daily_goal"
        self._attr_device_info = waterh_device_info(coordinator)
        self._attr_native_unit_of_measurement = ha_unit(unit)
        self._attr_native_min_value = from_ml(GOAL_MIN_ML, unit)
        self._attr_native_max_value = from_ml(GOAL_MAX_ML, unit)
        self._attr_native_step = 1 if unit == UNIT_OZ else 10

    @property
    def native_value(self) -> float | None:
        goal = self.coordinator.data.goal_ml if self.coordinator.data else None
        if goal is None:
            return None
        value = from_ml(goal, self._unit)
        return round(value) if self._unit == UNIT_OZ else value

    async def async_set_native_value(self, value: float) -> None:
        goal_ml = to_ml(value, self._unit)
        try:
            await self.coordinator.client.async_set_goal(goal_ml)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_goal_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
