"""Drink type picker for the WaterH "Log drink" button."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DRINK_TYPES
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import waterh_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the drink type select."""
    async_add_entities([WaterHQuickTypeSelect(entry.runtime_data)])


class WaterHQuickTypeSelect(CoordinatorEntity[WaterHCoordinator], SelectEntity, RestoreEntity):
    """Drink type the "Log drink" button uses. Stored in Home Assistant only."""

    _attr_has_entity_name = True
    _attr_translation_key = "quick_drink_type"
    _attr_options = DRINK_TYPES
    _attr_icon = "mdi:coffee-outline"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.user_id}_quick_drink_type"
        self._attr_device_info = waterh_device_info(coordinator)
        self._attr_current_option = coordinator.quick_drink_type

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in DRINK_TYPES:
            self._attr_current_option = last.state
            self.coordinator.quick_drink_type = last.state

    @property
    def current_option(self) -> str | None:
        return self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.coordinator.quick_drink_type = option
        self.async_write_ha_state()
