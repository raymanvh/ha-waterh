"""Sensors for WaterH."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_REPORTED, CONF_VOLUME_UNIT, UNIT_AUTO
from .coordinator import BottleStatus, WaterHConfigEntry, WaterHCoordinator, WaterHData
from .entity import waterh_device_info
from .units import ha_unit, resolve_unit


@dataclass(frozen=True, kw_only=True)
class WaterHSensorDescription(SensorEntityDescription):
    """Describes a WaterH sensor."""

    value_fn: Callable[[WaterHData], Any]
    attrs_fn: Callable[[WaterHData], dict[str, Any]] | None = None
    volume: bool = False
    daily_total: bool = False


def _drink_attrs(data: WaterHData) -> dict[str, Any]:
    drink = data.last_drink
    if drink is None:
        return {}
    return {
        "drink_id": drink.id,
        "source": drink.source_id,
        "water_type": drink.water_type,
        "liquid_amount_ml": drink.amount,
        "temperature": drink.temperature,
    }


def _today_attrs(data: WaterHData) -> dict[str, Any]:
    return {
        "drinks": len(data.drinks),
        "bottle_ml": data.bottle_total_ml,
        "manual_ml": data.manual_total_ml,
        "liquid_total_ml": data.liquid_total_ml,
        "day_start": data.day_start.isoformat() if data.day_start else None,
    }


def _last_time(data: WaterHData) -> datetime | None:
    return data.last_drink.created if data.last_drink else None


SENSORS: tuple[WaterHSensorDescription, ...] = (
    WaterHSensorDescription(
        key="water_today",
        translation_key="water_today",
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        suggested_display_precision=0,
        volume=True,
        daily_total=True,
        value_fn=lambda d: d.total_ml,
        attrs_fn=_today_attrs,
    ),
    WaterHSensorDescription(
        key="goal_progress",
        translation_key="goal_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.goal_progress,
    ),
    WaterHSensorDescription(
        key="drinks_today",
        translation_key="drinks_today",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.drinks),
    ),
    WaterHSensorDescription(
        key="last_drink_amount",
        translation_key="last_drink_amount",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        suggested_display_precision=0,
        volume=True,
        value_fn=lambda d: d.last_drink.water_amount if d.last_drink else None,
        attrs_fn=_drink_attrs,
    ),
    WaterHSensorDescription(
        key="last_drink_time",
        translation_key="last_drink_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_time,
    ),
    WaterHSensorDescription(
        key="last_water_temperature",
        translation_key="last_water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (
            d.last_bottle_drink.temperature if d.last_bottle_drink else None
        ),
    ),
    WaterHSensorDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.fetched_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WaterH sensors."""
    coordinator = entry.runtime_data
    option = entry.options.get(CONF_VOLUME_UNIT, UNIT_AUTO)
    async_add_entities(
        WaterHSensor(coordinator, description, option) for description in SENSORS
    )

    # Battery and firmware only come from the WaterH Sync Android app, so these
    # entities appear once it has reported for the first time.
    @callback
    def _add_bottle_sensors() -> None:
        async_add_entities(
            WaterHBottleSensor(coordinator, description) for description in BOTTLE_SENSORS
        )

    if entry.data.get(CONF_APP_REPORTED):
        _add_bottle_sensors()
    else:
        coordinator.async_add_first_report_callback(_add_bottle_sensors)


class WaterHSensor(CoordinatorEntity[WaterHCoordinator], SensorEntity):
    """A WaterH sensor."""

    _attr_has_entity_name = True
    entity_description: WaterHSensorDescription

    def __init__(
        self,
        coordinator: WaterHCoordinator,
        description: WaterHSensorDescription,
        unit_option: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.user_id}_{description.key}"
        self._unit_option = unit_option
        if description.volume:
            unit = ha_unit(self._resolved_unit())
            self._attr_suggested_unit_of_measurement = unit
            self._attr_suggested_display_precision = (
                1 if unit == UnitOfVolume.FLUID_OUNCES else 0
            )
        self._attr_device_info = waterh_device_info(coordinator)

    def _resolved_unit(self) -> str:
        profile = self.coordinator.data.profile if self.coordinator.data else None
        return resolve_unit(self.coordinator.hass, self._unit_option, profile)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The Volume unit option governs every volume entity, including ones
        # that already exist (suggested units only apply when an entity is
        # first created), so switching back to Automatic also takes effect.
        if not self.entity_description.volume or self.registry_entry is None:
            return
        unit = ha_unit(self._resolved_unit())
        current = dict(self.registry_entry.options.get("sensor", {}))
        if current.get("unit_of_measurement") == unit:
            return
        current["unit_of_measurement"] = unit
        er.async_get(self.hass).async_update_entity_options(
            self.entity_id, "sensor", current
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.daily_total and self.coordinator.data:
            return self.coordinator.data.day_start
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


@dataclass(frozen=True, kw_only=True)
class WaterHBottleSensorDescription(SensorEntityDescription):
    """Describes a sensor fed by the WaterH Sync Android app."""

    value_fn: Callable[[BottleStatus], Any]


BOTTLE_SENSORS: tuple[WaterHBottleSensorDescription, ...] = (
    WaterHBottleSensorDescription(
        key="bottle_battery",
        translation_key="bottle_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda b: b.battery,
    ),
    WaterHBottleSensorDescription(
        key="bottle_firmware",
        translation_key="bottle_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.firmware,
    ),
    WaterHBottleSensorDescription(
        key="bottle_reported",
        translation_key="bottle_reported",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.reported_at,
    ),
)


class WaterHBottleSensor(CoordinatorEntity[WaterHCoordinator], RestoreSensor):
    """A value reported by the Android app; keeps its last value across restarts."""

    _attr_has_entity_name = True
    entity_description: WaterHBottleSensorDescription

    def __init__(
        self, coordinator: WaterHCoordinator, description: WaterHBottleSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.user_id}_{description.key}"
        self._attr_device_info = waterh_device_info(coordinator)
        self._restored: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored = last.native_value

    @property
    def available(self) -> bool:
        # Reported by the app, not the cloud, so cloud outages don't affect it.
        return True

    @property
    def native_value(self) -> Any:
        bottle = self.coordinator.bottle
        if bottle is not None:
            return self.entity_description.value_fn(bottle)
        return self._restored
