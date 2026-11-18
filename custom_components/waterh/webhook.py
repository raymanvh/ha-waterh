"""Webhook the WaterH Sync Android app posts bottle status to.

The WaterH cloud has no battery or charging data; the bottle only sends them
over Bluetooth. The optional Android app reads them during each sync and
posts them here.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import BottleStatus, WaterHConfigEntry

_LOGGER = logging.getLogger(__name__)

PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Required("battery"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("charging"): vol.Any(None, bool),
        vol.Optional("firmware"): vol.Any(None, vol.All(str, vol.Length(max=32))),
        vol.Optional("today_ml"): vol.Any(
            None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100_000))
        ),
    },
    extra=vol.REMOVE_EXTRA,
)


@callback
def async_register_webhook(hass: HomeAssistant, entry: WaterHConfigEntry) -> None:
    """Register this entry's webhook, creating its id on first use."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = webhook.async_generate_id()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
        )

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        try:
            raw = await request.json()
            payload = PAYLOAD_SCHEMA(raw)
        except (ValueError, vol.Invalid) as err:
            _LOGGER.debug("Rejected WaterH Sync report: %s", err)
            return web.json_response({"ok": False, "error": "invalid payload"}, status=400)
        entry.runtime_data.async_set_bottle(
            BottleStatus(
                battery=payload["battery"],
                charging=payload.get("charging"),
                firmware=payload.get("firmware"),
                today_ml=payload.get("today_ml"),
                reported_at=dt_util.utcnow(),
            )
        )
        return web.json_response({"ok": True})

    webhook.async_register(
        hass,
        DOMAIN,
        f"WaterH Sync ({entry.title})",
        webhook_id,
        _handle,
        local_only=False,
        allowed_methods=["POST"],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))


def webhook_urls(hass: HomeAssistant, entry: WaterHConfigEntry) -> str:
    """Human-readable webhook URL(s) for the options form."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        return "(available after the integration has loaded once)"
    urls: list[str] = []
    for kwargs in ({"allow_internal": False}, {"allow_external": False}):
        try:
            url = webhook.async_generate_url(hass, webhook_id, allow_ip=True, **kwargs)
        except NoURLAvailableError:
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        return f"/api/webhook/{webhook_id} (set your Home Assistant URL under Settings → System → Network)"
    return "\n\n".join(urls)
