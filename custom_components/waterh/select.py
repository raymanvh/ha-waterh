"""Drink type picker for the WaterH "Log drink" button."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WaterHError
from .const import CONF_VOLUME_UNIT, DOMAIN, DRINK_TYPES, UNIT_AUTO, VOLUME_UNITS
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .entity import WaterHReminderEntity, waterh_device_info
from .protocol import (
    LIGHT_COLORS,
    LIGHT_EFFECT_OPTIONS,
    LIGHT_THEMES,
    REMINDER_INTERVALS,
    REMINDER_OFF,
    REMINDER_PERIODIC,
    REMINDER_SMART,
    shown_color,
)

REMINDER_MODES = {"off": REMINDER_OFF, "periodic": REMINDER_PERIODIC, "smart": REMINDER_SMART}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects."""
    entities: list[SelectEntity] = [
        WaterHQuickTypeSelect(entry.runtime_data),
        WaterHVolumeUnitSelect(entry.runtime_data, entry),
    ]
    if entry.runtime_data.ble is not None:
        entities.append(WaterHReminderModeSelect(entry.runtime_data))
        entities.append(WaterHReminderIntervalSelect(entry.runtime_data))
        entities.append(WaterHLightEffectSelect(entry.runtime_data))
        entities.append(WaterHLightColorSelect(entry.runtime_data))
    async_add_entities(entities)


class _WaterHLightSelect(WaterHReminderEntity, SelectEntity):
    """Reminder light settings. The bottle can't report them, so these show what
    Home Assistant last set, and are unknown until set here."""

    @property
    def available(self) -> bool:
        return self.coordinator.ble is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"waiting_for_bottle": self.coordinator.light_pending}

    async def _async_light(self, effect: str | None = None, rgb: str | None = None) -> None:
        try:
            await self.coordinator.async_set_light(effect, rgb)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_light_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class WaterHLightEffectSelect(_WaterHLightSelect):
    """How the light ring looks when it reminds you."""

    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator, "light_effect")
        self._attr_options = list(LIGHT_EFFECT_OPTIONS)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.light_effect

    async def async_select_option(self, option: str) -> None:
        await self._async_light(effect=option)


class WaterHLightColorSelect(_WaterHLightSelect):
    """Colour of the light ring, for Breathe and Calm.

    Unavailable while a theme (rainbow pulse, warmth, christmas) is selected,
    as themes have their own colours.
    """

    _attr_icon = "mdi:palette"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator, "light_color")
        self._attr_options = list(LIGHT_COLORS)

    @property
    def current_option(self) -> str | None:
        # What the ring shows (a colour stored by an earlier version may not be
        # an exact preset, but the bottle only ever shows one of them).
        return shown_color(self.coordinator.light_rgb)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.light_effect not in LIGHT_THEMES

    async def async_select_option(self, option: str) -> None:
        await self._async_light(rgb=LIGHT_COLORS[option])


class WaterHReminderIntervalSelect(WaterHReminderEntity, SelectEntity):
    """Minutes between reminders (periodic) or without a drink before one (smart).

    A list rather than a number: the bottle only accepts these values and
    ignores the whole reminder setting for any other.
    """

    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator, "reminder_interval")
        self._attr_options = [str(i) for i in REMINDER_INTERVALS]

    @property
    def options(self) -> list[str]:
        options = [str(i) for i in REMINDER_INTERVALS]
        reminder = self.coordinator.reminder
        # Show a value the bottle has that isn't in the list (set some other way).
        if reminder is not None and str(reminder.interval_minutes) not in options:
            options.append(str(reminder.interval_minutes))
        return options

    @property
    def current_option(self) -> str | None:
        reminder = self.coordinator.reminder
        return str(reminder.interval_minutes) if reminder else None

    async def async_select_option(self, option: str) -> None:
        await self._async_change(interval_minutes=int(option))


class WaterHReminderModeSelect(WaterHReminderEntity, SelectEntity):
    """How the bottle's light ring reminds you: off, periodic or smart."""

    _attr_icon = "mdi:bell-ring-outline"

    def __init__(self, coordinator: WaterHCoordinator) -> None:
        super().__init__(coordinator, "reminder_mode")
        self._attr_options = list(REMINDER_MODES)

    @property
    def current_option(self) -> str | None:
        reminder = self.coordinator.reminder
        if reminder is None:
            return None
        return next((k for k, v in REMINDER_MODES.items() if v == reminder.mode), None)

    async def async_select_option(self, option: str) -> None:
        await self._async_change(mode=REMINDER_MODES[option])


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


class WaterHVolumeUnitSelect(CoordinatorEntity[WaterHCoordinator], SelectEntity):
    """Volume unit option, mirrored from the integration options.

    Changing it updates the config entry options, which reloads the
    integration so every entity switches unit.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "volume_unit"
    _attr_options = VOLUME_UNITS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator: WaterHCoordinator, entry: WaterHConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.user_id}_volume_unit"
        self._attr_device_info = waterh_device_info(coordinator)

    @property
    def current_option(self) -> str | None:
        return self._entry.options.get(CONF_VOLUME_UNIT, UNIT_AUTO)

    async def async_select_option(self, option: str) -> None:
        if option == self.current_option:
            return
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_VOLUME_UNIT: option}
        )
