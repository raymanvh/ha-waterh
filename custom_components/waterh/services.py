"""Actions (services) for WaterH."""

from __future__ import annotations

from datetime import datetime

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import WaterHError
from .const import (
    ATTR_AMOUNT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRINK_ID,
    ATTR_DRINK_TYPE,
    ATTR_TIME,
    ATTR_UNIT,
    DOMAIN,
    DRINK_TYPES,
    GOAL_MAX_ML,
    SERVICE_DELETE_DRINK,
    SERVICE_LOG_DRINK,
    UNIT_ML,
    UNIT_OZ,
)
from .coordinator import WaterHConfigEntry
from .units import entry_unit, to_ml

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
            await coordinator.client.async_add_drink(
                coordinator.user_id, amount_ml, call.data[ATTR_DRINK_TYPE], when
            )
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="log_drink_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await coordinator.async_request_refresh()

    async def delete_drink(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        coordinator = entry.runtime_data
        try:
            await coordinator.client.async_delete_drink(call.data[ATTR_DRINK_ID])
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="delete_drink_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_LOG_DRINK, log_drink, schema=LOG_DRINK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_DRINK, delete_drink, schema=DELETE_DRINK_SCHEMA
    )
