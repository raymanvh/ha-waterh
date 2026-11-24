"""The WaterH integration.

Three ways for drinks to reach Home Assistant (see README):
- bluetooth: Home Assistant connects to the bottle itself.
- local: the WaterH Sync Android app sends drinks to a webhook.
- cloud: drinks are read from the WaterH account the official app uploads to.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import WaterHApiClient, WaterHTokens
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DOMAIN,
)
from .coordinator import WaterHConfigEntry, WaterHCoordinator
from .local import LocalDrinkStore
from .services import async_setup_services
from .settings import (
    android_app_enabled,
    bottle_address,
    is_no_account,
    sync_interval,
    update_bottle,
)
from .webhook import async_register_webhook

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.TIME,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register WaterH actions."""
    async_setup_services(hass)
    return True


def client_credentials(entry: WaterHConfigEntry) -> tuple[str, str]:
    """Return the OAuth client credentials, preferring user overrides."""
    client_id = entry.options.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID
    client_secret = entry.options.get(CONF_CLIENT_SECRET) or DEFAULT_CLIENT_SECRET
    return client_id, client_secret


async def async_setup_entry(hass: HomeAssistant, entry: WaterHConfigEntry) -> bool:
    """Set up WaterH from a config entry."""
    address = bottle_address(entry)
    if address and "bluetooth" not in hass.config.components:
        raise ConfigEntryNotReady(
            "Home Assistant's Bluetooth integration isn't set up. Add a Bluetooth "
            "adapter or an ESPHome Bluetooth proxy, or turn Bluetooth sync off in "
            "this integration's options."
        )
    if is_no_account(entry):
        store = LocalDrinkStore(hass, entry.entry_id)
        await store.async_load()
        coordinator = WaterHCoordinator(hass, entry, None, store)
    else:
        coordinator = _cloud_coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if android_app_enabled(entry):
        # Receives reports (and, without an account, drinks) from the WaterH Sync app.
        async_register_webhook(hass, entry)

    if address:
        from .bluetooth_sync import BottleBluetooth

        coordinator.ble = BottleBluetooth(
            hass,
            entry,
            coordinator,
            address,
            sync_interval(entry),
            update_bottle(entry),
        )

    # 0.5.0 had the reminder interval as a number; it's a list now (the bottle
    # only accepts certain values), so remove the old entity.
    ent_reg = er.async_get(hass)
    if old_interval := ent_reg.async_get_entity_id(
        "number", DOMAIN, f"{coordinator.user_id}_reminder_interval"
    ):
        ent_reg.async_remove(old_interval)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.ble is not None:
        entry.async_on_unload(coordinator.ble.async_start())

    # Token refreshes also update the entry, so only reload when the options
    # themselves change.
    options_at_setup = dict(entry.options)

    async def _async_reload_on_options(
        hass: HomeAssistant, updated: WaterHConfigEntry
    ) -> None:
        if dict(updated.options) != options_at_setup:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


def _cloud_coordinator(hass: HomeAssistant, entry: WaterHConfigEntry) -> WaterHCoordinator:
    client_id, client_secret = client_credentials(entry)

    async def _save_tokens(tokens: WaterHTokens) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_TOKEN_EXPIRES_AT: tokens.expires_at,
            },
        )

    client = WaterHApiClient(
        async_get_clientsession(hass),
        client_id,
        client_secret,
        tokens=WaterHTokens(
            access_token=entry.data[CONF_ACCESS_TOKEN],
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            expires_at=float(entry.data.get(CONF_TOKEN_EXPIRES_AT, 0)),
        ),
        on_tokens_updated=_save_tokens,
        time_zone=hass.config.time_zone,
    )

    return WaterHCoordinator(hass, entry, client)


async def async_unload_entry(hass: HomeAssistant, entry: WaterHConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
