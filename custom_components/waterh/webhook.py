"""Webhook the WaterH Sync Android app posts to.

Every mode: bottle status (battery, charging, firmware), which WaterH's cloud
doesn't have. Local mode: also the drinks read off the bottle, which are
stored before the reply so the app only clears the bottle once they're safe.

Reply: {"ok", "mode", "accepted", "goal_ml", "unit"}. The app uses goal_ml
for the bottle and unit for display. An empty JSON object just fetches those.
"""

from __future__ import annotations

import logging
from datetime import datetime

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .api import WaterHDrink
from .const import DOMAIN, MODE_CLOUD, MODE_LOCAL, SOURCE_BOTTLE
from .coordinator import BottleStatus, WaterHConfigEntry
from .local import bottle_drink_id
from .units import entry_unit

_LOGGER = logging.getLogger(__name__)

DRINK_SCHEMA = vol.Schema(
    {
        # Epoch milliseconds, converted on the phone from the bottle's local clock.
        vol.Required("time"): vol.All(vol.Coerce(int), vol.Range(min=1_500_000_000_000)),
        vol.Required("amount_ml"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5000)),
        vol.Optional("tds"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("temperature"): vol.Any(None, vol.Coerce(float)),
    },
    extra=vol.REMOVE_EXTRA,
)

PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional("battery"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("charging"): vol.Any(None, bool),
        vol.Optional("firmware"): vol.Any(None, vol.All(str, vol.Length(max=32))),
        vol.Optional("today_ml"): vol.Any(
            None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100_000))
        ),
        vol.Optional("source"): vol.All(str, vol.Length(max=64)),
        vol.Optional("drinks"): vol.All([DRINK_SCHEMA], vol.Length(max=5000)),
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
        coordinator = entry.runtime_data
        mode = MODE_LOCAL if coordinator.is_local else MODE_CLOUD
        accepted = 0

        if coordinator.is_local and "drinks" not in payload:
            # Any check-in counts as an update in local mode; drink reports record it themselves.
            await coordinator.async_note_app_contact()

        if "drinks" in payload:
            if not coordinator.is_local:
                # Never let the app clear the bottle for drinks we can't keep.
                return web.json_response(
                    {"ok": False, "mode": mode, "error": "This WaterH integration is in cloud mode; drinks go to WaterH."},
                    status=409,
                )
            source = payload.get("source") or SOURCE_BOTTLE
            drinks = []
            for d in payload["drinks"]:
                created = datetime.fromtimestamp(d["time"] / 1000, tz=dt_util.UTC)
                drinks.append(
                    WaterHDrink(
                        id=bottle_drink_id(created, d["amount_ml"]),
                        water_amount=d["amount_ml"],
                        amount=d["amount_ml"],
                        created=created,
                        source_id=source,
                        mac_address=None,
                        water_type="water",
                        temperature=d.get("temperature"),
                        tds=d.get("tds"),
                    )
                )
            accepted = await coordinator.async_receive_bottle_drinks(drinks)

        if "battery" in payload:
            coordinator.async_set_bottle(
                BottleStatus(
                    battery=payload["battery"],
                    charging=payload.get("charging"),
                    firmware=payload.get("firmware"),
                    today_ml=payload.get("today_ml"),
                    reported_at=dt_util.utcnow(),
                )
            )

        data = coordinator.data
        return web.json_response(
            {
                "ok": True,
                "mode": mode,
                "accepted": accepted,
                "goal_ml": data.goal_ml if data else None,
                "unit": entry_unit(hass, entry),
            }
        )

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
    return webhook_urls_for_id(hass, webhook_id)


def webhook_urls_for_id(hass: HomeAssistant, webhook_id: str) -> str:
    """Human-readable webhook URL(s) for a webhook id."""
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
