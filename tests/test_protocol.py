"""Protocol tests, ported from the Android app (packets captured from a real Boost 946 mL)."""

# The bottle's clock is naive local time, so these tests use naive datetimes.
# ruff: noqa: DTZ001

from __future__ import annotations

from datetime import datetime

from custom_components.waterh.protocol import (
    REQUEST_RECALIBRATE_EMPTY,
    REQUEST_STATUS,
    REQUEST_WATER_LOGS,
    BoostStatus,
    BottleTime,
    LogCollector,
    LogsFound,
    LogStart,
    NoLogs,
    Other,
    RecalibrateReport,
    SettingsAck,
    clear_records,
    parse_packet,
    set_today_total,
    sync_settings,
)

REAL_STATUS_CHARGING = (
    "5250002702026307031A0B1117000203040A0F030510FB051000730011021701072601090001003C022A01"
)
REAL_STATUS_OFF_CHARGER = (
    "5250002702026307031A0B1117020303040AC5030510FB051000730011021700072601090001003C022A01"
)
REAL_LOG_REPLY = (
    "5054002928061A0B1117011D007E00000163001A0B11170126001D00000163001A0B11170133001B0000016300"
)


def record(y, mo, d, h, mi, s, ml, tds, temp, flag=0) -> bytes:
    return bytes([y, mo, d, h, mi, s, ml >> 8, ml & 0xFF, tds >> 8, tds & 0xFF, temp, 0, flag])


def test_commands_match_official_app() -> None:
    assert REQUEST_STATUS.hex().upper() == "47540001FF"
    assert REQUEST_WATER_LOGS.hex().upper() == "4754000106"


def test_real_boost_status() -> None:
    p = parse_packet(bytes.fromhex(REAL_STATUS_CHARGING))
    assert isinstance(p, BoostStatus)
    assert p.battery == 99
    assert p.charging is True
    assert p.firmware == "1.1.5"
    assert p.clock.to_naive() == datetime(2026, 11, 17, 23, 0, 2)
    assert p.today_ml == 2575
    assert p.goal_ml == 4347
    assert p.reminder_hex == "01090001003c"

    q = parse_packet(bytes.fromhex(REAL_STATUS_OFF_CHARGER))
    assert isinstance(q, BoostStatus)
    assert q.charging is False
    assert q.today_ml == 2757


def test_sync_settings_matches_official_format() -> None:
    cmd = sync_settings(4347, datetime(2026, 11, 17, 23, 5, 9), "01090001003C")
    assert cmd.hex().upper() == (
        "505400140305" + "10FB" + "0703" + "1A0B1117" + "0509" + "0726" + "01090001003C"
    )
    assert len(cmd) == 24


def test_set_today_total() -> None:
    assert set_today_total(2206).hex().upper() == "505400040304089E"
    assert set_today_total(0).hex().upper() == "5054000403040000"
    assert parse_packet(bytes.fromhex("52500003020400")) == Other(
        "today total saved", "52500003020400"
    )


def test_clear_command() -> None:
    assert clear_records(3).hex().upper() == "5250000403060027"


def test_real_log_reply() -> None:
    p = parse_packet(bytes.fromhex(REAL_LOG_REPLY))
    assert isinstance(p, LogStart)
    assert p.total_records == 3
    assert [r.amount_ml for r in p.records] == [126, 29, 27]
    assert p.records[0].time.to_naive() == datetime(2026, 11, 17, 23, 1, 29)


def test_short_status_is_not_a_crash() -> None:
    assert isinstance(parse_packet(bytes([0x52, 0x50, 0, 39, 0, 0, 50])), Other)
    assert isinstance(parse_packet(b""), Other)


def test_no_logs_and_logs_found() -> None:
    assert parse_packet(bytes([0x52, 0x50, 0, 3, 2, 6, 0])) == NoLogs()
    assert parse_packet(bytes([0x52, 0x50, 0, 3, 2, 6, 1])) == LogsFound()


def test_settings_ack() -> None:
    assert isinstance(parse_packet(bytes.fromhex("5250000F0305" + "10FB" + "00" * 13)), SettingsAck)


def test_multi_packet_log() -> None:
    r1 = record(26, 11, 17, 20, 1, 9, ml=237, tds=12, temp=21)
    r2 = record(26, 11, 17, 20, 5, 58, ml=59, tds=13, temp=20)
    flagged = record(26, 11, 17, 20, 6, 0, ml=0, tds=0, temp=0, flag=1)
    c = LogCollector()
    assert not c.add(parse_packet(bytes([0x50, 0x54, 0x00, 39, 13, 6]) + r1))
    assert c.expected == 3
    assert not c.add(parse_packet(bytes([13, 6]) + r2))
    assert c.add(parse_packet(bytes([13, 6]) + flagged))
    assert [d.amount_ml for d in c.drinks] == [237, 59]
    assert c.drinks[0].tds == 12 and c.drinks[0].temperature_raw == 21
    assert c.drinks[0].time.to_naive() == datetime(2026, 11, 17, 20, 1, 9)
    assert len(c.records) == 3


def test_several_records_in_one_large_packet() -> None:
    r1 = record(26, 11, 17, 8, 0, 0, ml=300, tds=5, temp=18)
    r2 = record(26, 11, 17, 9, 0, 0, ml=400, tds=5, temp=18)
    p = parse_packet(bytes([0x50, 0x54, 0x00, 26, 26, 6]) + r1 + r2 + bytes([1, 2]))
    assert isinstance(p, LogStart)
    assert p.total_records == 2
    assert [r.amount_ml for r in p.records] == [300, 400]


def test_invalid_clock_gives_none() -> None:
    assert BottleTime(0, 0, 0, 0, 0, 0).to_naive() is None


def test_recalibrate_command_and_reports() -> None:
    # BottleConnection.requestRecalibrate(empty = true) in the official app.
    assert REQUEST_RECALIBRATE_EMPTY.hex().upper() == "5054000302A101"
    # RT report, tag 0xA1, code: 2 started, 3 succeeded, 4 failed.
    assert parse_packet(bytes.fromhex("5254000302A102")) == RecalibrateReport(2)
    assert parse_packet(bytes.fromhex("5254000302A103")) == RecalibrateReport(3)
    assert parse_packet(bytes.fromhex("5254000302A104")) == RecalibrateReport(4)
    # Other live reports are still ignored.
    assert isinstance(parse_packet(bytes.fromhex("52540003020240")), Other)


def test_reminder_settings() -> None:
    from custom_components.waterh.protocol import (
        REMINDER_PERIODIC,
        REMINDER_SMART,
        ReminderSettings,
        set_reminder,
    )

    # From the real status packet: periodic, 09:00 to 01:00, every 60 minutes.
    r = ReminderSettings.from_hex("01090001003c")
    assert r == ReminderSettings(REMINDER_PERIODIC, 9, 0, 1, 0, 60)
    assert r.describe() == "periodic every 60 min, 09:00-01:00"
    assert r.to_hex() == "01090001003c"
    # Same layout as the official app's Reminder.toBottleFormData().
    smart = ReminderSettings(REMINDER_SMART, 8, 30, 22, 15, 45)
    assert smart.to_hex() == "02081e160f2d"
    assert set_reminder(smart.to_hex()).hex().upper() == "50540008072602081E160F2D"
    assert ReminderSettings.from_hex(None) is None
    assert ReminderSettings.from_hex("0102") is None
    import pytest

    for bad in (
        ReminderSettings(REMINDER_PERIODIC, 24, 0, 1, 0, 60),
        ReminderSettings(REMINDER_PERIODIC, 9, 60, 1, 0, 60),
        ReminderSettings(REMINDER_PERIODIC, 9, 0, 1, 0, 0),
        ReminderSettings(REMINDER_PERIODIC, 9, 0, 1, 0, 241),
        ReminderSettings(7, 9, 0, 1, 0, 60),
    ):
        with pytest.raises(ValueError):
            bad.to_hex()


def test_reminder_light_matches_official_app() -> None:
    from custom_components.waterh.protocol import (
        LIGHT_COLORS,
        LIGHT_EFFECTS,
        set_reminder_light,
    )

    # BottleConnection.requestReminderLight: "5054000605fb" + mode + colour.
    c = LIGHT_COLORS
    assert set_reminder_light("breathe", c["red"]).hex() == "5054000605fb01ff0000"
    assert set_reminder_light("default_blue", c["blue"]).hex() == "5054000605fb000000ff"
    from custom_components.waterh.protocol import LIGHT_EFFECT_OPTIONS, LIGHT_THEMES

    assert "default_blue" not in LIGHT_EFFECT_OPTIONS  # looks like breathe + blue
    assert set(LIGHT_THEMES) == {"rainbow_pulse", "warmth", "christmas"}
    assert set_reminder_light("calm", c["purple"]).hex() == "5054000605fb02ff00ff"
    assert set_reminder_light("christmas", c["white"]).hex() == "5054000605fb06ffffff"
    from custom_components.waterh.protocol import shown_color

    # Seen on a Boost: each channel is on or off.
    assert shown_color("ff69b4") == "white"
    assert shown_color("ff8000") == "yellow"
    assert shown_color("ff0000") == "red"
    assert shown_color("000000") is None
    assert shown_color(None) is None
    # Mode bytes from the app's WhenMappings (there is no 04).
    assert LIGHT_EFFECTS == {
        "default_blue": 0, "breathe": 1, "calm": 2, "rainbow_pulse": 3, "warmth": 5, "christmas": 6,
    }
    assert len(LIGHT_COLORS) == 7


def test_control_light_command() -> None:
    from custom_components.waterh.protocol import CONTROL_LIGHT

    # BottleConnection.requestControlLight in the official app.
    assert CONTROL_LIGHT.hex() == "50540003021d01"
