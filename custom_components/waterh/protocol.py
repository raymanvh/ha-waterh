"""WaterH bottle Bluetooth protocol.

A Python port of the WaterH Sync Android app's protocol code, itself taken
from the official WaterH Android app (3.5.1). See docs/PROTOCOL.md in the
waterh-sync-android repository for the full write-up.

The bottle exposes a serial-style pair of characteristics: commands are
written to WRITE_CHAR and every reply arrives as a notification on
NOTIFY_CHAR. Multi-byte numbers are big-endian. No pairing is needed.

Everything here is pure (no Bluetooth, no Home Assistant) so it can be tested
on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

WRITE_SERVICE = "0000ffe5-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"
NOTIFY_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"

# "GT" read requests: these only ask for data and change nothing on the bottle.
REQUEST_STATUS = bytes.fromhex("47540001ff")
REQUEST_WATER_LOGS = bytes.fromhex("4754000106")

# Recalibrate with the bottle empty, as the official app's "Recalibrate bottle"
# screen does (BottleConnection.requestRecalibrate, empty = true). The bottle
# answers with RT reports tagged 0xA1; see RECALIBRATE_* below.
REQUEST_RECALIBRATE_EMPTY = bytes.fromhex("5054000302A101")
RECALIBRATE_STARTED = 2
RECALIBRATE_SUCCEEDED = 3
RECALIBRATE_FAILED = 4

# Reminder settings (status field 38 and the 0x26 settings tag), 6 bytes:
# [mode][start hour][start minute][end hour][end minute][interval minutes].
# Built like the official app's Reminder.toBottleFormData(): mode 00 off,
# 01 periodic (light up every interval), 02 smart (light up if nothing was
# drunk for an interval).
REMINDER_OFF = 0
REMINDER_PERIODIC = 1
REMINDER_SMART = 2
# Intervals the bottle accepts: the official app's bottle-light picker
# (reminder_intervals in DrinkReminderTimeIntervalSelector). Tested on a Boost:
# 15 and 60 are applied, while 2 and 59 make it ignore the whole reminder
# setting. (The app's 1-60/120/180/240 list is a different, phone-side picker.)
REMINDER_INTERVALS = (15, 30, 45, 60, 90, 120)


@dataclass(frozen=True)
class ReminderSettings:
    """When the bottle's light ring reminds you to drink."""

    mode: int
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    interval_minutes: int

    @classmethod
    def from_hex(cls, value: str | None) -> ReminderSettings | None:
        if not value or len(value) != 12:
            return None
        try:
            return cls(*bytes.fromhex(value))
        except ValueError:
            return None

    def to_hex(self) -> str:
        for hour in (self.start_hour, self.end_hour):
            if not 0 <= hour <= 23:
                raise ValueError("hour must be 0-23")
        for minute in (self.start_minute, self.end_minute):
            if not 0 <= minute <= 59:
                raise ValueError("minute must be 0-59")
        if self.interval_minutes not in REMINDER_INTERVALS:
            raise ValueError(
                "the bottle only accepts reminder intervals of "
                + ", ".join(str(i) for i in REMINDER_INTERVALS)
                + " minutes"
            )
        if self.mode not in (REMINDER_OFF, REMINDER_PERIODIC, REMINDER_SMART):
            raise ValueError("unknown reminder mode")
        return bytes(
            [
                self.mode,
                self.start_hour,
                self.start_minute,
                self.end_hour,
                self.end_minute,
                self.interval_minutes,
            ]
        ).hex()

    def describe(self) -> str:
        mode = {REMINDER_OFF: "off", REMINDER_PERIODIC: "periodic", REMINDER_SMART: "smart"}
        return (
            f"{mode.get(self.mode, f'mode {self.mode}')} every {self.interval_minutes} min, "
            f"{self.start_hour:02d}:{self.start_minute:02d}-"
            f"{self.end_hour:02d}:{self.end_minute:02d}"
        )


def set_reminder(reminder_hex: str) -> bytes:
    """Set only the reminder (official app: requestUpdateReminder)."""
    if len(reminder_hex) != 12:
        raise ValueError("reminder must be 6 bytes")
    return bytes.fromhex("505400080726" + reminder_hex)


# Reminder light ring: effect and colour (official app: requestReminderLight).
# Command 5054000605fb + effect (1 byte) + colour (3 bytes, RGB). The bottle
# never reports which effect or colour it has.
LIGHT_EFFECTS = {
    "default_blue": 0x00,
    "breathe": 0x01,
    "calm": 0x02,
    "rainbow_pulse": 0x03,
    "warmth": 0x05,
    "christmas": 0x06,
}
# The official app only shows a colour choice for these effects; the others
# have their own colours and ignore it (seen on a Boost: default blue looks
# like breathe in blue; rainbow pulse, warmth and christmas have their own).
# The app still sends the last colour picked.
LIGHT_EFFECTS_WITH_COLOR = ("breathe", "calm")
LIGHT_COLORS = {
    "red": "ff0000",
    "yellow": "ffff00",
    "green": "00ff00",
    "cyan": "00ffff",
    "blue": "0000ff",
    "purple": "ff00ff",
    "white": "ffffff",
}
# The official app's defaults for a new bottle.
DEFAULT_LIGHT_EFFECT = "default_blue"
DEFAULT_LIGHT_COLOR = "blue"


def set_reminder_light(effect: str, rgb_hex: str) -> bytes:
    """Set the light ring's effect and colour (6 hex digits, e.g. LIGHT_COLORS["red"]).

    The official app only sends the seven LIGHT_COLORS; whether the bottle
    shows other RGB values is untested.
    """
    rgb = bytes.fromhex(rgb_hex)
    if len(rgb) != 3:
        raise ValueError("colour must be 3 bytes (RRGGBB)")
    return bytes.fromhex("5054000605fb" + f"{LIGHT_EFFECTS[effect]:02x}") + rgb


# Experimental: "control light" (official app: requestControlLight), sent when
# you tap the bottle in its bottle-details screen. Tested on a Boost: the ring
# flashes blue, whatever the reminder light is set to, and the reminder light
# setting is unchanged afterwards.
CONTROL_LIGHT = bytes.fromhex("50540003021d01")

# Size of one stored record in a log reply.
RECORD_SIZE = 13


def sync_settings(goal_ml: int, now: datetime, reminder_hex: str) -> bytes:
    """Set the bottle's clock, goal and reminder in one command.

    The official app sends this on every connect. Pass the bottle's own
    reminder (from its status reply) to leave reminders unchanged.
    """
    if len(reminder_hex) != 12:
        raise ValueError("reminder must be 6 bytes")
    clock = "".join(
        f"{v:02x}"
        for v in (now.year % 100, now.month, now.day, now.hour, now.minute, now.second)
    )
    return bytes.fromhex(
        "505400140305" + f"{goal_ml & 0xFFFF:04x}" + "0703" + clock + "0726" + reminder_hex
    )


def set_today_total(ml: int) -> bytes:
    """Set the bottle's own "today" total (status field 4), in mL."""
    return bytes.fromhex("505400040304" + f"{max(0, min(ml, 0xFFFF)):04x}")


def clear_records(total_records: int) -> bytes:
    """Delete [total_records] records (drinks plus flagged ones) from the bottle."""
    return bytes.fromhex("525000040306" + f"{(total_records * RECORD_SIZE) & 0xFFFF:04x}")


@dataclass(frozen=True)
class BottleTime:
    """A date and time on the bottle's own clock (local time, 2-digit year)."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int

    def to_naive(self) -> datetime | None:
        """The time as a naive local datetime, or None if the clock was never set."""
        try:
            # The bottle's clock has no time zone; it's set to local time.
            return datetime(  # noqa: DTZ001
                2000 + self.year, self.month, self.day, self.hour, self.minute, self.second
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class LogRecord:
    """One 13-byte record from the bottle's stored log.

    Byte 12 is a flag: 0 is a drink; anything else is a record that counts
    toward the total to clear but isn't a drink.
    """

    time: BottleTime
    amount_ml: int
    tds: int
    temperature_raw: int
    flag: int

    @property
    def is_drink(self) -> bool:
        return self.flag == 0


@dataclass(frozen=True)
class BoostStatus:
    """Status reply from a Boost bottle."""

    battery: int
    charging: bool
    firmware: str
    clock: BottleTime
    today_ml: int | None
    goal_ml: int | None
    reminder_hex: str | None


@dataclass(frozen=True)
class VitaStatus:
    """Status reply from a Vita bottle (layout not decoded yet)."""

    hex: str


@dataclass(frozen=True)
class LogStart:
    """First packet of a stored-log reply; [total_records] covers all packets."""

    total_records: int
    records: list[LogRecord]


@dataclass(frozen=True)
class LogContinued:
    """A follow-on packet of a stored-log reply."""

    records: list[LogRecord]


@dataclass(frozen=True)
class NoLogs:
    """The bottle has no stored records."""


@dataclass(frozen=True)
class LogsFound:
    """The bottle has stored records and is about to send them."""


@dataclass(frozen=True)
class SettingsAck:
    """Acknowledgement of the clock/goal/reminder command."""

    goal_ml: int


@dataclass(frozen=True)
class RecalibrateReport:
    """Progress of a recalibration: 2 started, 3 succeeded, 4 failed."""

    code: int


@dataclass(frozen=True)
class Other:
    """Anything not interpreted here (live reports, registration, ...)."""

    description: str
    hex: str


Packet = (
    BoostStatus
    | VitaStatus
    | LogStart
    | LogContinued
    | NoLogs
    | LogsFound
    | SettingsAck
    | RecalibrateReport
    | Other
)


def _u(data: bytes, i: int) -> int | None:
    return data[i] if 0 <= i < len(data) else None


def _be16(data: bytes, i: int) -> int | None:
    if i < 0 or i + 1 >= len(data):
        return None
    return (data[i] << 8) | data[i + 1]


def _time(data: bytes, i: int) -> BottleTime | None:
    if i < 0 or i + 5 >= len(data):
        return None
    return BottleTime(*data[i : i + 6])


def parse_tlv(value: bytes, start: int) -> dict[int, bytes]:
    """Decode [length][tag][value...] fields; length counts the tag plus value."""
    out: dict[int, bytes] = {}
    i = start
    while i + 1 < len(value):
        length = value[i]
        if length < 1 or i + 1 + length > len(value):
            break
        out[value[i + 1]] = value[i + 2 : i + 1 + length]
        i += 1 + length
    return out


def parse_records(value: bytes, start: int) -> list[LogRecord]:
    """Decode the 13-byte records from [start]; partial trailing bytes are ignored."""
    records: list[LogRecord] = []
    i = start
    while i + RECORD_SIZE <= len(value):
        records.append(
            LogRecord(
                time=BottleTime(*value[i : i + 6]),
                amount_ml=(value[i + 6] << 8) | value[i + 7],
                tds=(value[i + 8] << 8) | value[i + 9],
                temperature_raw=value[i + 10],
                flag=value[i + 12],
            )
        )
        i += RECORD_SIZE
    return records


def parse_packet(value: bytes) -> Packet:
    """Decode one notification, in the same branch order as the official app."""
    hex_ = value.hex().upper()
    if not value:
        return Other("empty", hex_)
    b0, b1 = value[0], _u(value, 1)

    # "RP": responses to commands.
    if b0 == 0x52 and b1 == 0x50:
        if _u(value, 5) == 0x20:
            return Other("signature response", hex_)
        if _u(value, 2) == 0 and _u(value, 3) == 49:
            return VitaStatus(hex_)
        if _u(value, 2) == 0 and _u(value, 3) == 39:
            fields = parse_tlv(value, 4)
            battery = fields[2][0] if fields.get(2) else None
            clock = _time(fields[3], 0) if len(fields.get(3, b"")) >= 6 else None
            version = _be16(fields.get(16, b""), 0)
            if battery is None or clock is None or version is None:
                return Other("short status", hex_)
            charging = fields[23][0] if fields.get(23) else None
            reminder = fields.get(38)
            return BoostStatus(
                battery=battery,
                charging=charging in (1, 2),
                firmware=f"{version // 100}.{(version % 100) // 10}.{version % 10}",
                clock=clock,
                today_ml=_be16(fields.get(4, b""), 0),
                goal_ml=_be16(fields.get(5, b""), 0),
                reminder_hex=reminder.hex() if reminder and len(reminder) == 6 else None,
            )
        if _u(value, 2) == 0 and _u(value, 3) == 15:
            return SettingsAck(_be16(value, 6) or 0)
        tag = _u(value, 5)
        if tag == 6:
            flag = _u(value, 6)
            if flag == 0:
                return NoLogs()
            if flag == 1:
                return LogsFound()
            return Other("water log response", hex_)
        descriptions = {
            38: "reminder response",
            5: "goal response",
            4: "today total saved",
            28: "registration response",
        }
        return Other(descriptions.get(tag, "response"), hex_)  # type: ignore[arg-type]

    # "RT": live reports. Only recalibration progress (tag 0xA1) is used here;
    # temperature, volume and the rest aren't needed for syncing.
    if b0 == 0x52 and b1 == 0x54:
        if _u(value, 5) == 0xA1 and (code := _u(value, 6)) is not None:
            return RecalibrateReport(code)
        return Other("live report", hex_)

    # "PT" with command 6: first packet of the stored log.
    if b0 == 0x50 and b1 == 0x54:
        if _u(value, 5) == 6:
            total = (_be16(value, 2) or 0) // RECORD_SIZE
            return LogStart(total, parse_records(value, 6))
        return Other("PT packet", hex_)

    # Follow-on log packet: byte 0 is this packet's payload length, byte 1 is 6.
    if b1 == 6:
        return LogContinued(parse_records(value, 2))

    return Other("unknown", hex_)


@dataclass
class LogCollector:
    """Collects a multi-packet stored-log reply.

    Complete once the records received (drinks plus flagged records) reach the
    total announced in the first packet, as in the official app.
    """

    expected: int | None = None
    records: list[LogRecord] = field(default_factory=list)

    @property
    def drinks(self) -> list[LogRecord]:
        return [r for r in self.records if r.is_drink]

    def add(self, packet: Packet) -> bool:
        """Add a log packet. Returns True when the reply is complete."""
        if isinstance(packet, LogStart):
            self.expected = packet.total_records
            self.records = list(packet.records)
        elif isinstance(packet, LogContinued):
            if self.expected is None:
                return False
            self.records.extend(packet.records)
        else:
            return False
        return self.expected is not None and len(self.records) >= self.expected
