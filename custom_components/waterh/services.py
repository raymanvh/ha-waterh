"""Actions (services) for WaterH."""

from __future__ import annotations

from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import WaterHError
from .const import (
    ATTR_AMOUNT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRINK_ID,
    ATTR_DRINK_TYPE,
    ATTR_END,
    ATTR_START,
    ATTR_TIME,
    ATTR_UNIT,
    DOMAIN,
    DRINK_TYPES,
    GOAL_MAX_ML,
    SERVICE_DELETE_DRINK,
    SERVICE_GET_DRINKS,
    SERVICE_LOG_DRINK,
    SERVICE_SET_REMINDER_LIGHT,
    UNIT_ML,
    UNIT_OZ,
)
from .coordinator import WaterHConfigEntry
from .protocol import LIGHT_COLORS, LIGHT_EFFECT_OPTIONS
from .records import drink_record
from .units import entry_unit, from_ml, ha_unit, to_ml

LOG_DRINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_AMOUNT): vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False)),
        vol.Optional(ATTR_UNIT): vol.In([UNIT_ML, UNIT_OZ]),
        vol.Optional(ATTR_DRINK_TYPE, default="water"): vol.In(DRINK_TYPES),
        vol.Optional(ATTR_TIME): cv.datetime,
    }
)

DELETE_DRINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_DRINK_ID): cv.string,
    }
)

GET_DRINKS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
    }
)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


SET_REMINDER_LIGHT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("effect"): vol.In(list(LIGHT_EFFECT_OPTIONS)),
        vol.Optional("color"): vol.In(list(LIGHT_COLORS)),
    }
)


def _get_entry(hass: HomeAssistant, call: ServiceCall) -> WaterHConfigEntry:
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entry"
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="multiple_entries"
        )
    return entries[0]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the WaterH actions."""

    async def log_drink(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        coordinator = entry.runtime_data
        unit = call.data.get(ATTR_UNIT) or entry_unit(hass, entry)
        amount_ml = to_ml(call.data[ATTR_AMOUNT], unit)
        if amount_ml < 1 or amount_ml > GOAL_MAX_ML:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_amount",
                translation_placeholders={"amount_ml": str(amount_ml)},
            )
        when: datetime | None = call.data.get(ATTR_TIME)
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.get_default_time_zone())
        try:
            await coordinator.async_add_drink(
                amount_ml, call.data[ATTR_DRINK_TYPE], when
            )
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="log_drink_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def delete_drink(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        coordinator = entry.runtime_data
        try:
            await coordinator.async_delete_drink(call.data[ATTR_DRINK_ID])
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="delete_drink_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def get_drinks(call: ServiceCall) -> ServiceResponse:
        entry = _get_entry(hass, call)
        coordinator = entry.runtime_data
        start = _aware(call.data.get(ATTR_START)) or dt_util.start_of_local_day()
        # Default end includes drinks a bottle clock running slightly ahead put in the future.
        end = _aware(call.data.get(ATTR_END)) or dt_util.now() + timedelta(days=1)
        if end <= start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_range"
            )
        try:
            drinks = await coordinator.async_get_drinks(start, end)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="get_drinks_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        unit = entry_unit(hass, entry)
        total_ml = sum(d.water_amount for d in drinks)
        return {
            "start": dt_util.as_local(start).isoformat(),
            "end": dt_util.as_local(end).isoformat(),
            "unit": ha_unit(unit),
            "total": from_ml(total_ml, unit),
            "total_ml": total_ml,
            "drinks": [drink_record(d, unit) for d in drinks],
        }

    async def set_reminder_light(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        coordinator = entry.runtime_data
        rgb = LIGHT_COLORS[call.data["color"]] if "color" in call.data else None
        try:
            if coordinator.ble is None:
                raise WaterHError(
                    "The reminder light needs Bluetooth through Home Assistant."
                )
            await coordinator.async_set_light(call.data.get("effect"), rgb)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_light_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    hass.services.async_register(
        DOMAIN, SERVICE_LOG_DRINK, log_drink, schema=LOG_DRINK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_REMINDER_LIGHT,
        set_reminder_light,
        schema=SET_REMINDER_LIGHT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_DRINK, delete_drink, schema=DELETE_DRINK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DRINKS,
        get_drinks,
        schema=GET_DRINKS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
