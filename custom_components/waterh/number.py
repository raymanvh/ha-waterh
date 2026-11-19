"""Number entities for WaterH: daily goal and the quick-add drink amount."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WaterHError
from .const import DOMAIN, UNIT_OZ
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import waterh_device_info
from .units import entry_unit, from_ml, ha_unit, to_ml

# Limits per display unit. Min, max and step must line up (the frontend only
# accepts min + n * step), so they are whole numbers in each unit rather than
# converted from the other unit.
GOAL_LIMITS = {UNIT_OZ: (17, 338, 1), "ml": (500, 10000, 10)}
QUICK_LIMITS = {UNIT_OZ: (0.1, 100, 0.1), "ml": (1, 3000, 1)}
QUICK_DEFAULT = {UNIT_OZ: 8.0, "ml": 250.0}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WaterH number entities."""
    unit = entry_unit(hass, entry)
    coordinator = entry.runtime_data
    async_add_entities(
        [WaterHGoalNumber(coordinator, unit), WaterHQuickAmountNumber(coordinator, unit)]
    )


class _WaterHNumberBase(CoordinatorEntity[WaterHCoordinator]):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: WaterHCoordinator, unit: str, key: str) -> None:
        super().__init__(coordinator)
        self._unit = unit
        self._attr_unique_id = f"{coordinator.user_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = waterh_device_info(coordinator)
        self._attr_native_unit_of_measurement = ha_unit(unit)


class WaterHGoalNumber(_WaterHNumberBase, NumberEntity):
    """Daily hydration goal, editable from Home Assistant."""

    _attr_icon = "mdi:cup-water"

    def __init__(self, coordinator: WaterHCoordinator, unit: str) -> None:
        super().__init__(coordinator, unit, "daily_goal")
        low, high, step = GOAL_LIMITS[unit]
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step

    @property
    def native_value(self) -> float | None:
        goal = self.coordinator.data.goal_ml if self.coordinator.data else None
        if goal is None:
            return None
        if self._unit == UNIT_OZ:
            return round(from_ml(goal, UNIT_OZ))
        return float(goal)

    async def async_set_native_value(self, value: float) -> None:
        goal_ml = to_ml(value, self._unit)
        try:
            await self.coordinator.async_set_goal(goal_ml)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_goal_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class WaterHQuickAmountNumber(_WaterHNumberBase, RestoreNumber):
    """Amount the "Log drink" button adds. Stored in Home Assistant only."""

    _attr_icon = "mdi:cup-outline"

    def __init__(self, coordinator: WaterHCoordinator, unit: str) -> None:
        super().__init__(coordinator, unit, "quick_amount")
        low, high, step = QUICK_LIMITS[unit]
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_value = QUICK_DEFAULT[unit]
        coordinator.quick_amount_ml = to_ml(QUICK_DEFAULT[unit], unit)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if (
            last is not None
            and last.native_value is not None
            and last.native_unit_of_measurement == self.native_unit_of_measurement
            and self.native_min_value <= last.native_value <= self.native_max_value
        ):
            self._attr_native_value = last.native_value
            self.coordinator.quick_amount_ml = to_ml(last.native_value, self._unit)

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.coordinator.quick_amount_ml = to_ml(value, self._unit)
        self.async_write_ha_state()
