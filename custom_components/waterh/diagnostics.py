"""Diagnostics for WaterH."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BOTTLE_ADDRESS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_WEBHOOK_ID,
)
from .coordinator import WaterHConfigEntry

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_EMAIL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_WEBHOOK_ID,
    CONF_USER_ID,
    CONF_NAME,
    CONF_BOTTLE_ADDRESS,
    "id",
    "email",
    "first_name",
    "last_name",
    "username",
    "avatar",
    "dob",
    "birthday",
    "gender",
    "phone",
    "mac_address",
    "source_id",
    "height",
    "weight",
}

# Profile keys safe to include as-is. Everything else from /account/self/ is
# left out (only its key names are listed), so fields WaterH adds later can't
# leak personal data into a diagnostics download.
PROFILE_SAFE_KEYS = {"goal"}


def _profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Return only non-personal profile fields that help with debugging."""
    settings = profile.get("settings")
    return {
        **{key: profile[key] for key in PROFILE_SAFE_KEYS if key in profile},
        "unit": settings.get("unit") if isinstance(settings, dict) else None,
        "keys": sorted(profile),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WaterHConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    ble = coordinator.ble
    bluetooth: dict[str, Any] | None = None
    if ble is not None:
        result = ble.last_result
        bluetooth = {
            "status": ble.status,
            "in_range": ble.in_range,
            "interval_minutes": ble.interval.total_seconds() / 60,
            "update_bottle": ble.update_bottle,
            "reminder": {
                "chosen_in_home_assistant": coordinator.store.reminder_hex
                if coordinator.store
                else None,
                "last_on_bottle": coordinator.store.bottle_reminder_hex
                if coordinator.store
                else None,
                "pending": coordinator.reminder_pending,
            },
            "reminder_light": {
                "effect": coordinator.light_effect,
                "rgb": coordinator.light_rgb,
                "pending": coordinator.light_pending,
            },
            "last_attempt": ble.last_attempt.isoformat() if ble.last_attempt else None,
            "last_success": ble.last_success.isoformat() if ble.last_success else None,
            "last_error": ble.last_error,
            "last_session": {
                "drinks": len(result.drinks),
                "records_on_bottle": result.records_on_bottle,
                "complete": result.complete,
                "cleared": result.cleared,
                "settings_written": result.settings_written,
                "reminder_written": result.reminder_written,
                "light_written": result.light_written,
                "total_written": result.total_written,
                "steps": result.steps,
            }
            if result
            else None,
        }
    return {
        "bluetooth": bluetooth,
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
        "profile": _profile_summary(data.profile),
    }
