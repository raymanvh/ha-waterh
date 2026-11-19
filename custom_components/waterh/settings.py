"""Read a config entry's mode and sync settings (data plus options, with defaults)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_ANDROID_APP,
    CONF_BOTTLE_ADDRESS,
    CONF_MODE,
    CONF_SYNC_INTERVAL,
    CONF_UPDATE_BOTTLE,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    MODE_CLOUD,
    MODE_LOCAL,
    NO_ACCOUNT_MODES,
)


def entry_mode(entry: ConfigEntry) -> str:
    """bluetooth, local (Android app) or cloud. Entries from before modes existed are cloud."""
    return entry.data.get(CONF_MODE, MODE_CLOUD)


def is_no_account(entry: ConfigEntry) -> bool:
    return entry_mode(entry) in NO_ACCOUNT_MODES


def bottle_address(entry: ConfigEntry) -> str | None:
    """Bottle Home Assistant syncs over Bluetooth, or None when that's off."""
    if not is_no_account(entry):
        return None
    if CONF_BOTTLE_ADDRESS in entry.options:
        address = entry.options[CONF_BOTTLE_ADDRESS]
    else:
        address = entry.data.get(CONF_BOTTLE_ADDRESS)
    return address.upper() if address else None


def android_app_enabled(entry: ConfigEntry) -> bool:
    """Whether the WaterH Sync Android app's webhook is open for this entry.

    Cloud entries always accept the app's battery reports. Android-app entries
    default on; Bluetooth entries default off until the user turns it on.
    """
    mode = entry_mode(entry)
    if mode == MODE_CLOUD:
        return True
    return bool(entry.options.get(CONF_ANDROID_APP, mode == MODE_LOCAL))


def sync_interval(entry: ConfigEntry) -> timedelta:
    minutes = entry.options.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL_MINUTES)
    return timedelta(minutes=int(minutes))


def update_bottle(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(CONF_UPDATE_BOTTLE, True))
