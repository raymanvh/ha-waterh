"""Diagnostics for WaterH."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
)
from .coordinator import WaterHConfigEntry

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_EMAIL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    "email",
    "first_name",
    "last_name",
    "username",
    "avatar",
    "dob",
    "mac_address",
    "height",
    "weight",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WaterHConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    data = entry.runtime_data.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "today": {
            "total_ml": data.total_ml,
            "goal_ml": data.goal_ml,
            "drinks": [
                async_redact_data(
                    {**asdict(d), "created": d.created.isoformat()}, TO_REDACT
                )
                for d in data.drinks
            ],
        },
        "profile": async_redact_data(data.profile, TO_REDACT),
    }
