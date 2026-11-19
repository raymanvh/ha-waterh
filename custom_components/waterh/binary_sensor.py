"""Binary sensors for WaterH: charging (read from the bottle) and bottle in range."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_REPORTED
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import waterh_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the in-range sensor, and the charging sensor once the bottle has been read."""
    coordinator = entry.runtime_data
    if coordinator.ble is not None:
        async_add_entities([WaterHInRangeSensor(coordinator)])

    @callback
    def _add() -> None:
        async_add_entities([WaterHChargingSensor(coordinator)])

    if entry.data.get(CONF_APP_REPORTED):
        _add()
    else:
        coordinator.async_add_first_report_callback(_add)


class WaterHInRangeSensor(CoordinatorEntity[WaterHCoordinator], BinarySensorEntity):
    """Whether Home Assistant can currently hear the bottle over Bluetooth."""

    _attr_has_entity_name = True
    _attr_translation_key = "in_range"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.user_id}_bottle_in_range"
        self._attr_device_info = waterh_device_info(coordinator)

    @property
    def available(self) -> bool:
        return self.coordinator.ble is not None

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.ble and self.coordinator.ble.in_range)


class WaterHChargingSensor(CoordinatorEntity[WaterHCoordinator], BinarySensorEntity, RestoreEntity):
    """Whether the bottle is on its charger, as of the last Bluetooth read."""

    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.user_id}_bottle_charging"
        self._attr_device_info = waterh_device_info(coordinator)
        self._restored: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._restored = last.state == STATE_ON

    @property
    def available(self) -> bool:
        # Read from the bottle, not the cloud, so cloud outages don't affect it.
        return True

    @property
    def is_on(self) -> bool | None:
        bottle = self.coordinator.bottle
        if bottle is not None and bottle.charging is not None:
            return bottle.charging
        return self._restored
