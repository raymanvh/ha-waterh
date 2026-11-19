"""Tests for the WaterH integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.waterh.api import parse_api_datetime
from custom_components.waterh.const import (
    API_BASE_URL,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_USER_ID,
    DOMAIN,
)

from .conftest import SELF_RESPONSE, TOKEN_RESPONSE, log_response

TOKEN_URL = f"{API_BASE_URL}/account/oauth/token/"
SELF_URL = f"{API_BASE_URL}/account/self/"
LOG_URL = f"{API_BASE_URL}/account/123456/log"


def test_parse_api_datetime() -> None:
    parsed = parse_api_datetime("2026-11-18T00:22:40.000000Z")
    assert parsed == datetime(2026, 11, 18, 0, 22, 40, tzinfo=timezone.utc)


async def test_config_flow_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(LOG_URL, json=log_response([]))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: " user@example.com ", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    data = result["data"]
    assert data[CONF_USER_ID] == 123456
    assert data[CONF_ACCESS_TOKEN] == "access-1"
    assert data[CONF_REFRESH_TOKEN] == "refresh-1"
    assert CONF_PASSWORD not in data

    # The login request carried the app credentials and the password grant.
    method, url, body, _ = aioclient_mock.mock_calls[0]
    assert body["grant_type"] == "password"
    assert body["email"] == "user@example.com"
    assert body["client_id"] and body["client_secret"]


async def test_config_flow_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


def _entry(expires_at: float) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        title="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_USER_ID: 123456,
            CONF_ACCESS_TOKEN: "access-1",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_TOKEN_EXPIRES_AT: expires_at,
        },
    )


async def test_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/Chicago")
    hass.config.units = US_CUSTOMARY_SYSTEM
    freezer.move_to("2026-11-18T00:45:00+00:00")  # 18:45 on Nov 17 in Chicago

    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(
        LOG_URL,
        json=log_response(
            [
                ("2026-11-17T23:27:49.000000Z", 945, "manual"),
                ("2026-11-17T23:57:09.000000Z", 107, "waterh-boost-946ml"),
                ("2026-11-18T00:00:15.000000Z", 96, "waterh-boost-946ml"),
                ("2026-11-18T00:22:40.000000Z", 68, "waterh-boost-946ml"),
            ]
        ),
    )
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Log request uses local-midnight boundaries in seconds.
    log_calls = [c for c in aioclient_mock.mock_calls if "/log" in str(c[1])]
    query = log_calls[0][1].query
    start = dt_util.as_local(datetime.fromtimestamp(int(query["date_finished_from"]), timezone.utc))
    assert (start.hour, start.minute, start.day) == (0, 0, 17)
    assert int(query["date_finished_to"]) - int(query["date_finished_from"]) == 86400

    total = hass.states.get("sensor.waterh_test_user_water_today")
    assert total is not None
    # 1216 mL shown as fl. oz. on a US-unit system (matches the app's 41.1 oz).
    assert total.attributes["unit_of_measurement"] == "fl. oz."
    assert float(total.state) == pytest.approx(41.1, abs=0.05)
    assert total.attributes["drinks"] == 4
    assert total.attributes["bottle_ml"] == 271
    assert total.attributes["manual_ml"] == 945

    progress = hass.states.get("sensor.waterh_test_user_goal_progress")
    assert float(progress.state) == pytest.approx(28.0, abs=0.1)

    count = hass.states.get("sensor.waterh_test_user_drinks_today")
    assert count.state == "4"

    last_time = hass.states.get("sensor.waterh_test_user_last_drink_time")
    assert last_time.state == "2026-11-18T00:22:40+00:00"

    # Diagnostic "Last update" records each successful fetch, even when
    # nothing else changed.
    last_update = hass.states.get("sensor.waterh_test_user_last_update")
    assert last_update.state == "2026-11-18T00:45:00+00:00"
    freezer.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    last_update = hass.states.get("sensor.waterh_test_user_last_update")
    assert last_update.state == "2026-11-18T00:50:00+00:00"
    assert hass.states.get("sensor.waterh_test_user_drinks_today").state == "4"


async def test_expired_token_is_refreshed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        TOKEN_URL, json={**TOKEN_RESPONSE, "access_token": "access-2", "refresh_token": "refresh-2"}
    )
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(LOG_URL, json=log_response([]))

    entry = _entry(expires_at=0)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_URL]
    assert token_calls[0][2]["grant_type"] == "refresh_token"
    assert token_calls[0][2]["refresh_token"] == "refresh-1"
    assert entry.data[CONF_ACCESS_TOKEN] == "access-2"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-2"
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_rejected_refresh_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, status=401, json={"error": "invalid_grant"})
    entry = _entry(expires_at=0)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def _setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, options: dict | None = None
) -> MockConfigEntry:
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(LOG_URL, json=log_response([]))
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    if options:
        entry = MockConfigEntry(
            domain=entry.domain,
            unique_id=entry.unique_id,
            title=entry.title,
            data=entry.data,
            options=options,
        )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_log_drink_action(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(LOG_URL, json=log_response([]))

    await hass.services.async_call(
        DOMAIN,
        "log_drink",
        {
            "amount": 8,
            "unit": "oz",
            "drink_type": "coffee",
            "time": "2026-11-17 18:30:00",
        },
        blocking=True,
    )
    post = [c for c in aioclient_mock.mock_calls if c[0] == "POST" and "/log" in str(c[1])]
    assert len(post) == 1
    assert post[0][2]["ble_data"] == []
    entry = post[0][2]["data"][0]
    assert entry["amount"] == 237  # 8 fl. oz.
    assert entry["water_type"] == "coffee"
    # Local 18:30 in the test time zone, sent as UTC like the app does.
    sent = parse_api_datetime(entry["date_created"])
    assert sent == dt_util.as_utc(
        datetime(2026, 11, 17, 18, 30, tzinfo=dt_util.get_default_time_zone())
    )
    assert entry["date_created"].endswith("+00:00")
    assert set(entry) == {"amount", "date_created", "water_type"}


async def test_log_drink_rejects_bad_amount(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    await _setup(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "log_drink", {"amount": 50000, "unit": "ml"}, blocking=True
        )


async def test_delete_drink_action(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.delete(f"{API_BASE_URL}/v2/log/123", status=204)
    await hass.services.async_call(
        DOMAIN, "delete_drink", {"drink_id": "123"}, blocking=True
    )
    assert any(
        c[0] == "DELETE" and str(c[1]).endswith("/v2/log/123")
        for c in aioclient_mock.mock_calls
    )


async def test_goal_number_metric_and_set(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    state = hass.states.get("number.waterh_test_user_daily_goal")
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == 4347

    aioclient_mock.patch(SELF_URL, json={**SELF_RESPONSE, "goal": 3000})
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.waterh_test_user_daily_goal", "value": 3000},
        blocking=True,
    )
    patch = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch[0][2] == {"goal": 3000}


async def test_unit_option_oz_on_metric_system(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock, options={"volume_unit": "oz", "scan_interval": 5})
    goal = hass.states.get("number.waterh_test_user_daily_goal")
    assert goal.attributes["unit_of_measurement"] == "fl. oz."
    assert float(goal.state) == 147  # 4347 mL
    today = hass.states.get("sensor.waterh_test_user_water_today")
    assert today.attributes["unit_of_measurement"] == "fl. oz."

    aioclient_mock.patch(SELF_URL, json=SELF_RESPONSE)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.waterh_test_user_daily_goal", "value": 100},
        blocking=True,
    )
    patch = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch[0][2] == {"goal": 2957}


async def test_unit_option_follows_app(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        SELF_URL, json={**SELF_RESPONSE, "settings": {"unit": "imperial"}}
    )
    aioclient_mock.get(LOG_URL, json=log_response([]))
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data=_entry(dt_util.utcnow().timestamp() + 86400).data,
        options={"volume_unit": "app", "scan_interval": 5},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Metric HA, but the WaterH app is set to imperial.
    goal = hass.states.get("number.waterh_test_user_daily_goal")
    assert goal.attributes["unit_of_measurement"] == "fl. oz."


async def test_goal_number_limits_align_with_step(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Whole-number goals must be valid in the UI (min + n * step)."""
    await _setup(hass, aioclient_mock, options={"volume_unit": "oz", "scan_interval": 5})
    attrs = hass.states.get("number.waterh_test_user_daily_goal").attributes
    assert attrs["step"] == 1
    assert attrs["min"] == int(attrs["min"])
    assert (160 - attrs["min"]) % attrs["step"] == 0

    # Drink amount accepts one decimal in oz (e.g. 74.6), whole mL in metric.
    amount = hass.states.get("number.waterh_test_user_drink_amount").attributes
    assert amount["step"] == 0.1
    assert round((74.6 - amount["min"]) / amount["step"], 6) == 745


async def test_log_drink_button(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock, options={"volume_unit": "oz", "scan_interval": 5})
    amount = "number.waterh_test_user_drink_amount"
    drink_type = "select.waterh_test_user_drink_type"
    button = "button.waterh_test_user_log_drink"
    assert hass.states.get(amount).state == "8.0"
    assert hass.states.get(drink_type).state == "water"

    await hass.services.async_call(
        "number", "set_value", {"entity_id": amount, "value": 12}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": drink_type, "option": "coffee"}, blocking=True
    )
    aioclient_mock.post(LOG_URL, json=log_response([]))
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    post = [c for c in aioclient_mock.mock_calls if c[0] == "POST" and "/log" in str(c[1])]
    entry = post[0][2]["data"][0]
    assert entry["amount"] == 355  # 12 fl. oz.
    assert entry["water_type"] == "coffee"


async def test_volume_unit_select_switches_units(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock, options={"volume_unit": "ml", "scan_interval": 5})
    select = "select.waterh_test_user_volume_unit"
    assert hass.states.get(select).state == "ml"
    goal = "number.waterh_test_user_daily_goal"
    assert hass.states.get(goal).attributes["unit_of_measurement"] == "mL"

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "oz"}, blocking=True
    )
    await hass.async_block_till_done()

    assert entry.options["volume_unit"] == "oz"
    assert entry.options["scan_interval"] == 5
    assert hass.states.get(select).state == "oz"
    assert hass.states.get(goal).attributes["unit_of_measurement"] == "fl. oz."
    today = hass.states.get("sensor.waterh_test_user_water_today")
    assert today.attributes["unit_of_measurement"] == "fl. oz."


async def test_volume_unit_back_to_auto(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Switching oz -> automatic (metric HA) returns sensors to mL."""
    await _setup(hass, aioclient_mock, options={"volume_unit": "oz", "scan_interval": 5})
    select = "select.waterh_test_user_volume_unit"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "auto"}, blocking=True
    )
    await hass.async_block_till_done()
    today = hass.states.get("sensor.waterh_test_user_water_today")
    assert today.attributes["unit_of_measurement"] == "mL"


async def test_app_webhook_creates_bottle_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth,
) -> None:
    """Battery/charging appear only after the Android app reports."""
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(LOG_URL, json=log_response([]))
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    webhook_id = entry.data["webhook_id"]
    assert webhook_id
    # Cloud-only users never see the bottle entities.
    assert hass.states.get("sensor.waterh_test_user_bottle_battery") is None
    assert hass.states.get("binary_sensor.waterh_test_user_bottle_charging") is None

    client = await hass_client_no_auth()
    bad = await client.post(f"/api/webhook/{webhook_id}", json={"battery": 140})
    assert bad.status == 400
    assert hass.states.get("sensor.waterh_test_user_bottle_battery") is None

    ok = await client.post(
        f"/api/webhook/{webhook_id}",
        json={"battery": 87, "charging": True, "firmware": "1.1.5", "today_ml": 2757, "extra": 1},
    )
    assert ok.status == 200
    await hass.async_block_till_done()

    assert hass.states.get("sensor.waterh_test_user_bottle_battery").state == "87"
    assert hass.states.get("binary_sensor.waterh_test_user_bottle_charging").state == "on"
    assert hass.states.get("sensor.waterh_test_user_bottle_firmware").state == "1.1.5"
    assert entry.data["app_reported"] is True

    await client.post(f"/api/webhook/{webhook_id}", json={"battery": 86, "charging": False})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_test_user_bottle_battery").state == "86"
    assert hass.states.get("binary_sensor.waterh_test_user_bottle_charging").state == "off"

    # After a reload the entities exist straight away and keep their last values.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["webhook_id"] == webhook_id
    assert hass.states.get("sensor.waterh_test_user_bottle_battery").state == "86"
    assert hass.states.get("binary_sensor.waterh_test_user_bottle_charging").state == "off"


# ---------------------------------------------------------------------------
# Local mode


async def _setup_local(hass: HomeAssistant) -> MockConfigEntry:
    """Create a local entry through the real config flow and load it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"}
    )
    assert result["step_id"] == "local"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Bottle"}
    )
    assert result["step_id"] == "local_finish"
    assert "/api/webhook/" in result["description_placeholders"]["webhook_url"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mode"] == "local"
    assert "email" not in result["data"]
    await hass.async_block_till_done()
    return hass.config_entries.async_get_entry(result["result"].entry_id)


async def test_local_mode_drinks_from_app(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Drinks posted by the app are stored, deduplicated and drive the sensors."""
    await hass.config.async_set_time_zone("America/Chicago")
    freezer.move_to("2026-11-18T20:00:00-06:00")
    entry = await _setup_local(hass)
    webhook_id = entry.data["webhook_id"]
    client = await hass_client_no_auth()

    # No WaterH calls at all in local mode.
    assert aioclient_mock.call_count == 0
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "0"

    # Hello: empty payload returns goal and unit.
    hello = await (await client.post(f"/api/webhook/{webhook_id}", json={})).json()
    assert hello["ok"] and hello["mode"] == "local" and hello["goal_ml"] == 2000
    await hass.async_block_till_done()
    # Any check-in from the app counts as an update in local mode.
    assert hass.states.get("sensor.waterh_bottle_last_update").state == "2026-11-19T02:00:00+00:00"

    t1 = int(datetime(2026, 11, 18, 19, 1, 29, tzinfo=timezone(timedelta(hours=-6))).timestamp() * 1000)
    t2 = t1 + 9_000
    yesterday = t1 - 86_400_000
    drinks = [
        {"time": t1, "amount_ml": 126, "tds": 0, "temperature": 10},
        {"time": t2, "amount_ml": 29},
        {"time": yesterday, "amount_ml": 500},
    ]
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json={"drinks": drinks, "battery": 98, "charging": False, "source": "waterh-boost-946ml"},
    )
    body = await resp.json()
    assert resp.status == 200 and body["accepted"] == 3
    # The reply carries today's total (yesterday's drink excluded) for the bottle display.
    assert body["today_ml"] == 155
    await hass.async_block_till_done()

    assert hass.states.get("sensor.waterh_bottle_water_today").state == "155"
    assert hass.states.get("sensor.waterh_bottle_drinks_today").state == "2"
    assert hass.states.get("sensor.waterh_bottle_bottle_battery").state == "98"

    # Re-sending the same report (e.g. after a dropped reply) adds nothing.
    body = await (await client.post(f"/api/webhook/{webhook_id}", json={"drinks": drinks})).json()
    assert body["accepted"] == 0
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "155"

    # Goal is set in Home Assistant and handed back to the app.
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.waterh_bottle_daily_goal", "value": 3000},
        blocking=True,
    )
    hello = await (await client.post(f"/api/webhook/{webhook_id}", json={})).json()
    assert hello["goal_ml"] == 3000
    assert hass.states.get("sensor.waterh_bottle_goal_progress").state == "5.2"

    # Manual drinks and deletes work without WaterH.
    await hass.services.async_call(
        DOMAIN, "log_drink", {"amount": 250, "unit": "ml"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "405"
    drink_id = hass.states.get("sensor.waterh_bottle_last_drink").attributes["drink_id"]
    await hass.services.async_call(DOMAIN, "delete_drink", {"drink_id": drink_id}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "155"

    # Everything survives a reload (stored on disk).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "155"
    assert hass.states.get("number.waterh_bottle_daily_goal").state == "3000.0"

    # Midnight rolls the totals over.
    freezer.move_to("2026-11-19T00:01:00-06:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.waterh_bottle_water_today").state == "0"
    assert aioclient_mock.call_count == 0


async def test_cloud_mode_refuses_drinks(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth,
) -> None:
    """A cloud entry answers 409 to drinks, so the app keeps them on the bottle."""
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(LOG_URL, json=log_response([]))
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{entry.data['webhook_id']}",
        json={"drinks": [{"time": 1_795_000_000_000, "amount_ml": 100}]},
    )
    assert resp.status == 409
    assert (await resp.json())["mode"] == "cloud"
