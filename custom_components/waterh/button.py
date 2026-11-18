"""Log drink button for WaterH."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WaterHError
from .const import DOMAIN
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import waterh_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Log drink button."""
    async_add_entities([WaterHLogDrinkButton(entry.runtime_data)])


class WaterHLogDrinkButton(CoordinatorEntity[WaterHCoordinator], ButtonEntity):
    """Adds a manual drink using the "Drink amount" and "Drink type" controls."""

    _attr_has_entity_name = True
    _attr_translation_key = "log_drink"
    _attr_icon = "mdi:cup-water"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.user_id}_log_drink"
        self._attr_device_info = waterh_device_info(coordinator)

    async def async_press(self) -> None:
        coordinator = self.coordinator
        try:
            await coordinator.client.async_add_drink(
                coordinator.user_id,
                coordinator.quick_amount_ml,
                coordinator.quick_drink_type,
            )
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="log_drink_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await coordinator.async_request_refresh()
