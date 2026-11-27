"""Tests for the hourly water intake statistic (drinks at the time they were taken)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.waterh.const import ML_PER_FL_OZ
from custom_components.waterh.statistics import intake_statistic_id

from .conftest import SELF_RESPONSE, log_response
from .test_waterh import LOG_URL, SELF_URL, _entry, _setup_local

TZ = "America/Chicago"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Every test here needs the recorder, which must start before hass."""
    yield


def _ms(local_iso: str) -> int:
    return int(dt_util.parse_datetime(local_iso).timestamp() * 1000)


async def _rows(hass: HomeAssistant, statistic_id: str, start: str) -> dict[str, dict]:
    """Hourly rows keyed by local 'HH:MM' (or full date when not today)."""
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.parse_datetime(start),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state", "change"},
    )
    rows = {}
    for row in result.get(statistic_id, []):
        local = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
        rows[local.strftime("%m-%d %H:%M")] = row
    return rows


async def _post(hass, client, entry, drinks: list[tuple[str, int]]) -> None:
    resp = await client.post(
        f"/api/webhook/{entry.data['webhook_id']}",
        json={"drinks": [{"time": _ms(t), "amount_ml": ml} for t, ml in drinks]},
    )
    assert resp.status == 200
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_late_sync_lands_at_drink_time(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    """Drinks synced in one batch are charted in the hours they were drunk."""
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-26T18:32:00-06:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()
    stat_id = intake_statistic_id(entry)

    # Away from home all afternoon; everything arrives at 6:32 PM.
    await _post(
        hass,
        client,
        entry,
        [
            ("2026-11-25T23:30:00-06:00", 100),  # last night
            ("2026-11-26T10:05:00-06:00", 120),
            ("2026-11-26T10:40:00-06:00", 80),
            ("2026-11-26T14:20:00-06:00", 300),
        ],
    )
    rows = await _rows(hass, stat_id, "2026-11-25T22:00:00-06:00")
    assert rows["11-25 23:00"]["change"] == 100
    assert rows["11-26 00:00"]["change"] == 0
    assert rows["11-26 10:00"]["change"] == 200
    assert rows["11-26 14:00"]["change"] == 300
    assert rows["11-26 18:00"]["change"] == 0
    assert rows["11-26 18:00"]["sum"] == 600
    # state is today's running total; it starts over at midnight.
    assert rows["11-25 23:00"]["state"] == 100
    assert rows["11-26 09:00"]["state"] == 0
    assert rows["11-26 14:00"]["state"] == 500

    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={stat_id})
    )
    assert meta[stat_id][1]["unit_of_measurement"] == "mL"
    assert meta[stat_id][1]["has_sum"] is True

    # A drink from noon turns up later (another phone, a slow sync): the noon
    # hour fills in and every later total moves up with it.
    freezer.move_to("2026-11-26T19:10:00-06:00")
    await _post(hass, client, entry, [("2026-11-26T12:15:00-06:00", 50)])
    rows = await _rows(hass, stat_id, "2026-11-26T09:00:00-06:00")
    assert rows["11-26 12:00"]["change"] == 50
    assert rows["11-26 14:00"]["change"] == 300
    assert rows["11-26 19:00"]["sum"] == 650
    assert rows["11-26 19:00"]["state"] == 550

    # Deleting a drink takes it back out of its hour.
    drink_id = next(
        d.id for d in entry.runtime_data.store.drinks if d.water_amount == 300
    )
    await hass.services.async_call(
        "waterh", "delete_drink", {"drink_id": drink_id}, blocking=True
    )
    rows = await _rows(hass, stat_id, "2026-11-26T09:00:00-06:00")
    assert rows["11-26 14:00"]["change"] == 0
    assert rows["11-26 19:00"]["sum"] == 350

    # A new hour adds its row without anything else changing.
    freezer.move_to("2026-11-26T20:05:00-06:00")
    async_fire_time_changed(hass)
    rows = await _rows(hass, stat_id, "2026-11-26T09:00:00-06:00")
    assert rows["11-26 20:00"]["sum"] == 350
    assert rows["11-26 20:00"]["change"] == 0


async def test_fluid_ounces(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    """The statistic uses the integration's display unit."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-26T18:32:00-06:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()
    stat_id = intake_statistic_id(entry)

    await _post(hass, client, entry, [("2026-11-26T14:20:00-06:00", 300)])
    rows = await _rows(hass, stat_id, "2026-11-26T13:00:00-06:00")
    assert rows["11-26 14:00"]["change"] == pytest.approx(300 / ML_PER_FL_OZ)
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={stat_id})
    )
    assert meta[stat_id][1]["unit_of_measurement"] == "fl. oz."


async def test_old_hours_keep_their_totals(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    """Once drinks are pruned (30 days), earlier hours and the running sum stay put."""
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-10-20T12:30:00-05:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()
    stat_id = intake_statistic_id(entry)

    await _post(hass, client, entry, [("2026-10-20T09:10:00-05:00", 400)])
    before = await _rows(hass, stat_id, "2026-10-20T00:00:00-05:00")
    assert before["10-20 09:00"]["change"] == 400

    # 47 days on: that drink has been pruned from storage, a new one arrives.
    freezer.move_to("2026-12-06T12:30:00-06:00")
    async_fire_time_changed(hass)
    await _post(hass, client, entry, [("2026-12-06T11:00:00-06:00", 250)])
    assert all(d.water_amount != 400 for d in entry.runtime_data.store.drinks)

    after = await _rows(hass, stat_id, "2026-10-20T00:00:00-05:00")
    assert after["10-20 09:00"]["sum"] == 400
    assert after["12-06 11:00"]["change"] == 250
    assert after["12-06 11:00"]["sum"] == 650
    # No hour lost its intake when the old bucket rolled into the base.
    assert sum(r["change"] or 0 for r in after.values()) == pytest.approx(650)


async def test_unload_and_reload_keep_statistic(
    hass: HomeAssistant, hass_client_no_auth, freezer: FrozenDateTimeFactory
) -> None:
    """Bookkeeping survives a reload, so sums continue where they were."""
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-26T18:32:00-06:00")
    entry = await _setup_local(hass)
    client = await hass_client_no_auth()
    stat_id = intake_statistic_id(entry)
    await _post(hass, client, entry, [("2026-11-26T10:05:00-06:00", 120)])

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to("2026-11-26T19:05:00-06:00")
    client = await hass_client_no_auth()
    await _post(hass, client, entry, [("2026-11-26T19:01:00-06:00", 30)])
    rows = await _rows(hass, stat_id, "2026-11-26T09:00:00-06:00")
    assert rows["11-26 10:00"]["change"] == 120
    assert rows["11-26 19:00"]["sum"] == 150
    assert rows["11-26 19:00"]["change"] == 30


async def test_cloud_mode(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """Cloud drinks (today's, from WaterH) are charted at their own times too."""
    await hass.config.async_set_time_zone(TZ)
    freezer.move_to("2026-11-18T00:45:00+00:00")  # 18:45 on Nov 17 in Chicago
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(
        LOG_URL,
        json=log_response(
            [
                ("2026-11-17T15:10:00.000000Z", 200, "waterh-boost-946ml"),  # 09:10
                ("2026-11-17T23:27:49.000000Z", 945, "manual"),  # 17:27
            ]
        ),
    )
    entry = _entry(expires_at=dt_util.utcnow().timestamp() + 86400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    rows = await _rows(hass, intake_statistic_id(entry), "2026-11-17T00:00:00-06:00")
    assert rows["11-17 09:00"]["change"] == 200
    assert rows["11-17 17:00"]["change"] == 945
    assert rows["11-17 18:00"]["sum"] == 1145

    # Next day: yesterday's hours are kept, today's start from the new log.
    aioclient_mock.clear_requests()
    aioclient_mock.get(SELF_URL, json=SELF_RESPONSE)
    aioclient_mock.get(
        LOG_URL, json=log_response([("2026-11-18T14:00:00.000000Z", 300, "manual")])
    )
    freezer.move_to("2026-11-18T15:30:00+00:00")  # 09:30 on Nov 18
    await entry.runtime_data.async_refresh()
    rows = await _rows(hass, intake_statistic_id(entry), "2026-11-17T00:00:00-06:00")
    assert rows["11-17 17:00"]["change"] == 945
    assert rows["11-18 08:00"]["change"] == 300
    assert rows["11-18 09:00"]["sum"] == 1445
    assert rows["11-18 09:00"]["state"] == 300
