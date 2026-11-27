"""Tests for drinks at their exact times: Recent drinks sensor and waterh.get_drinks."""

from __future__ import annotations

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.waterh.const import DOMAIN

from .conftest import SELF_RESPONSE, log_response
from .test_waterh import LOG_URL, SELF_URL, _entry, _setup_local

TZ = "America/Chicago"
SENSOR = "sensor.waterh_bottle_recent_drinks"


def _ms(local_iso: str) -> int:
    return int(dt_util.parse_datetime(local_iso).timestamp() * 1000)


async def _post(hass, client, entry, drinks: list[tuple[str, int]]) -> None:
    resp = await client.post(
        f"/api/webhook/{entry.data['webhook_id']}",
        json={"drinks": [{"time": _ms(t), "amount_ml": ml} for t, ml in drinks]},
    )
    assert resp.status == 200
    await hass.async_block_till_done()


async def test_recent_drinks_sensor(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    """Lists each drink at its own time, for the configured number of days."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-26T18:32:00-06:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()

    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["days"] == 7
    assert state.attributes["drinks"] == []

    await _post(
        hass,
        client,
        entry,
        [
            ("2026-11-10T09:00:00-06:00", 500),  # older than 7 days: not listed
            ("2026-11-20T08:00:00-06:00", 200),  # 7th day back: listed
            ("2026-11-26T14:20:05-06:00", 300),
        ],
    )
    await hass.services.async_call(
        DOMAIN, "log_drink", {"amount": 8, "unit": "oz", "drink_type": "tea"}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state.state == "3"
    assert state.attributes["unit"] == "fl. oz."
    drinks = state.attributes["drinks"]
    assert [d["time"] for d in drinks] == [
        "2026-11-20T08:00:00-06:00",
        "2026-11-26T14:20:05-06:00",
        "2026-11-26T18:32:00-06:00",
    ]
    assert drinks[1]["ml"] == 300 and drinks[1]["amount"] == 10.1
    assert drinks[1]["source"] == "bottle" and drinks[1]["type"] == "water"
    assert drinks[2]["source"] == "manual" and drinks[2]["type"] == "tea"
    assert drinks[2]["ml"] == 237

    # The window moves at midnight.
    freezer.move_to("2026-11-27T00:00:30-06:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "2"

    # Fewer days through the options.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    if result["type"] == "menu":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "sync"}
        )
    assert result["step_id"] == "sync"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "address": "off",
            "android_app": True,
            "sync_interval": 5,
            "update_bottle": True,
            "volume_unit": "auto",
            "recent_days": 1,
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.attributes["days"] == 1
    assert state.state == "0"


async def test_get_drinks_local(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-26T18:32:00-06:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()
    await _post(
        hass,
        client,
        entry,
        [
            ("2026-11-25T23:30:00-06:00", 100),
            ("2026-11-26T10:05:00-06:00", 120),
            ("2026-11-26T14:20:00-06:00", 300),
        ],
    )

    # Default: today.
    result = await hass.services.async_call(
        DOMAIN, "get_drinks", {}, blocking=True, return_response=True
    )
    assert result["unit"] == "mL"
    assert result["total_ml"] == 420 and result["total"] == 420
    assert [d["time"] for d in result["drinks"]] == [
        "2026-11-26T10:05:00-06:00",
        "2026-11-26T14:20:00-06:00",
    ]

    # Any range (naive times are local); end not included.
    result = await hass.services.async_call(
        DOMAIN,
        "get_drinks",
        {"start": "2026-11-25 20:00:00", "end": "2026-11-26 14:20:00"},
        blocking=True,
        return_response=True,
    )
    assert [d["ml"] for d in result["drinks"]] == [100, 120]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_drinks",
            {"start": "2026-11-26 12:00:00", "end": "2026-11-26 11:00:00"},
            blocking=True,
            return_response=True,
        )


async def test_get_drinks_cloud_older_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """Account mode fetches days before today from WaterH."""
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-18T00:45:00+00:00")  # 18:45 on Nov 17 in Chicago
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(
        LOG_URL,
        json=log_response([("2026-11-17T15:10:00.000000Z", 200, "waterh-boost-946ml")]),
    )
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Account mode lists today's drinks only.
    state = hass.states.get("sensor.waterh_test_user_recent_drinks")
    assert state.state == "1" and state.attributes["days"] == 1

    # Today's come from the last update: no extra request.
    calls = aioclient_mock.call_count
    result = await hass.services.async_call(
        DOMAIN, "get_drinks", {}, blocking=True, return_response=True
    )
    assert [d["ml"] for d in result["drinks"]] == [200]
    assert aioclient_mock.call_count == calls

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        LOG_URL,
        json=log_response(
            [
                ("2026-11-15T14:00:00.000000Z", 350, "manual"),
                ("2026-11-17T15:10:00.000000Z", 200, "waterh-boost-946ml"),
            ]
        ),
    )
    result = await hass.services.async_call(
        DOMAIN,
        "get_drinks",
        {"start": "2026-11-15 00:00:00"},
        blocking=True,
        return_response=True,
    )
    assert aioclient_mock.call_count == 1
    assert [(d["time"], d["ml"], d["source"]) for d in result["drinks"]] == [
        ("2026-11-15T08:00:00-06:00", 350, "manual"),
        ("2026-11-17T09:10:00-06:00", 200, "bottle"),
    ]
