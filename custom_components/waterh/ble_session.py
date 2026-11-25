"""One Bluetooth sync session with a WaterH bottle.

Follows the same sequence as the official app and the WaterH Sync Android app:

1. enable notifications
2. ask for status (battery, clock, today's total, goal, reminder)
3. set the clock and goal (skipped when they're already right, or when the
   user turned bottle updates off)
4. ask for the drinks stored on the bottle
5. hand them to Home Assistant, which saves them to disk
6. only then clear them from the bottle, and set the bottle's own "today"
   total to match Home Assistant

The bottle ignores commands sent too quickly, so every command waits
[command_delay] (1 s, as the official app does) first. Nothing is cleared
from the bottle unless the full list of stored drinks arrived and was saved.

This module knows nothing about Home Assistant: it works with any connected
Bleak-style client, which keeps it testable with a fake bottle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .protocol import (
    CONTROL_LIGHT,
    NOTIFY_CHAR,
    RECALIBRATE_FAILED,
    RECALIBRATE_STARTED,
    RECALIBRATE_SUCCEEDED,
    REQUEST_RECALIBRATE_EMPTY,
    REQUEST_STATUS,
    REQUEST_WATER_LOGS,
    WRITE_CHAR,
    BoostStatus,
    LogCollector,
    LogContinued,
    LogRecord,
    LogStart,
    NoLogs,
    Other,
    Packet,
    RecalibrateReport,
    ReminderSettings,
    SettingsAck,
    VitaStatus,
    clear_records,
    parse_packet,
    set_reminder,
    set_reminder_light,
    set_today_total,
    sync_settings,
)

_LOGGER = logging.getLogger(__name__)

# Goals outside this range (mL) are never written to the bottle.
GOAL_RANGE_ML = range(500, 10_001)
# Leave the bottle's clock alone when it's within this many seconds of ours.
CLOCK_TOLERANCE_S = 30

STATUS_TIMEOUT_S = 6.0
SETTINGS_TIMEOUT_S = 6.0
# Per attempt; the stored-drinks request is sent up to twice.
LOGS_FIRST_TIMEOUT_S = 8.0
LOGS_NEXT_TIMEOUT_S = 5.0
ACK_TIMEOUT_S = 3.0
# How long to wait for the bottle to finish recalibrating.
RECALIBRATE_TIMEOUT_S = 30.0


class BottleSyncError(Exception):
    """A sync failed; the message is suitable for showing to the user."""


class BleClient(Protocol):
    """The parts of a Bleak client a session uses."""

    @property
    def services(self) -> Any: ...

    async def start_notify(self, char: str, callback: Callable[[Any, bytearray], None]) -> None: ...

    async def write_gatt_char(self, char: Any, data: bytes, response: bool = ...) -> None: ...


# Saves the drinks and returns the total Home Assistant has for today (mL),
# which the bottle's own total is set to. Raising means "not saved": the
# session then leaves everything on the bottle.
StoreDrinks = Callable[[list[LogRecord]], Awaitable[int | None]]


@dataclass
class SessionResult:
    """What one session did."""

    status: BoostStatus | None = None
    drinks: list[LogRecord] = field(default_factory=list)
    records_on_bottle: int = 0
    complete: bool = False
    cleared: int = 0
    settings_written: bool = False
    reminder_written: bool = False
    light_written: bool = False
    total_written: int | None = None
    steps: list[str] = field(default_factory=list)


class BottleSession:
    """Runs one sync over an already-connected client."""

    def __init__(
        self,
        client: BleClient,
        *,
        now: Callable[[], datetime],
        goal_ml: int | None,
        update_bottle: bool,
        store_drinks: StoreDrinks,
        command_delay: float = 1.0,
        reminder_hex: str | None = None,
        light: tuple[str, str] | None = None,
    ) -> None:
        self._client = client
        self._now = now
        self._goal_ml = goal_ml
        self._update_bottle = update_bottle
        self._store_drinks = store_drinks
        # Reminder chosen in Home Assistant (lowercase hex); None = keep the bottle's.
        self._reminder_hex = reminder_hex.lower() if reminder_hex else None
        # Reminder light (effect, RRGGBB) to send; None = no change.
        self._light = light
        self._delay = command_delay
        self._packets: asyncio.Queue[Packet] = asyncio.Queue()
        self._write_with_response = True
        self.result = SessionResult()

    def _step(self, text: str) -> None:
        _LOGGER.debug("WaterH bottle: %s", text)
        self.result.steps.append(text)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        packet = parse_packet(bytes(data))
        _LOGGER.debug("WaterH bottle ← %s (%s)", bytes(data).hex().upper(), type(packet).__name__)
        self._packets.put_nowait(packet)

    async def _send(self, command: bytes, label: str) -> None:
        await asyncio.sleep(self._delay)
        self._step(f"→ {label}")
        try:
            await self._client.write_gatt_char(
                WRITE_CHAR, command, response=self._write_with_response
            )
        except Exception as err:  # Bleak raises several types; all mean the same here.
            raise BottleSyncError(f"Couldn't send a command to the bottle ({label}): {err}") from err

    async def _wait_for(
        self, wanted: tuple[type, ...], timeout: float
    ) -> Packet | None:
        """Next packet of a wanted type, skipping others; None on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                packet = await asyncio.wait_for(self._packets.get(), remaining)
            except TimeoutError:
                return None
            if isinstance(packet, wanted):
                return packet
        return None

    async def run(self) -> SessionResult:
        """Run the session. Raises BottleSyncError when it can't finish."""
        await self._start()
        status = await self._read_status()
        await self._maybe_write_settings(status)
        await self._maybe_write_light()
        complete = await self._read_logs()
        await self._save_and_clear(status, complete)
        return self.result

    async def recalibrate(self) -> BoostStatus:
        """Recalibrate the empty bottle, as the official app's Recalibrate screen does.

        The bottle must be empty, capped, upright on a flat surface and not
        held. Stored drinks aren't touched. Raises BottleSyncError on failure.
        """
        await self._start()
        status = await self._read_status()
        await self._send(REQUEST_RECALIBRATE_EMPTY, "recalibrate (empty bottle)")
        started = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RECALIBRATE_TIMEOUT_S
        while (remaining := deadline - loop.time()) > 0:
            report = await self._wait_for((RecalibrateReport,), remaining)
            if report is None:
                break
            assert isinstance(report, RecalibrateReport)
            if report.code == RECALIBRATE_STARTED:
                started = True
                self._step("bottle started recalibrating")
            elif report.code == RECALIBRATE_SUCCEEDED:
                self._step("bottle recalibrated")
                return status
            elif report.code == RECALIBRATE_FAILED:
                raise BottleSyncError(
                    "The bottle reported that recalibration failed. Make sure it's empty, "
                    "the cap is on, and it's standing upright on a flat surface without "
                    "being touched, then try again."
                )
        if started:
            raise BottleSyncError(
                "The bottle started recalibrating but didn't report a result. "
                "Leave it still on a flat surface and try again."
            )
        raise BottleSyncError("The bottle didn't respond to the recalibrate request.")

    async def flash_light(self) -> None:
        """Experimental: send the official app's "control light" command.

        Any reminder light change made in Home Assistant goes first, so the
        flash shows it.
        """
        await self._start()
        await self._maybe_write_light()
        await self._send(CONTROL_LIGHT, "flash light ring (experimental)")
        reply = await self._wait_for((Other,), ACK_TIMEOUT_S)
        self._step(f"bottle replied {reply.hex}" if isinstance(reply, Other) else "no reply")

    async def _start(self) -> None:
        services = self._client.services
        write_char = services.get_characteristic(WRITE_CHAR) if services else None
        notify_char = services.get_characteristic(NOTIFY_CHAR) if services else None
        if write_char is None or notify_char is None:
            raise BottleSyncError("This device doesn't look like a WaterH bottle (no command channel).")
        properties = set(getattr(write_char, "properties", []) or [])
        self._write_with_response = "write" in properties or not properties

        try:
            await self._client.start_notify(NOTIFY_CHAR, self._on_notify)
        except Exception as err:
            raise BottleSyncError(f"Couldn't subscribe to the bottle's replies: {err}") from err

    async def _read_status(self) -> BoostStatus:
        for attempt in (1, 2):
            await self._send(REQUEST_STATUS, "request status" + (" (retry)" if attempt > 1 else ""))
            packet = await self._wait_for((BoostStatus, VitaStatus), STATUS_TIMEOUT_S)
            if isinstance(packet, VitaStatus):
                raise BottleSyncError("Vita bottles aren't supported yet (only the Boost).")
            if isinstance(packet, BoostStatus):
                self.result.status = packet
                self._step(
                    f"status: battery {packet.battery}%, today {packet.today_ml} mL, "
                    f"goal {packet.goal_ml} mL, firmware {packet.firmware}"
                )
                return packet
        raise BottleSyncError("The bottle connected but didn't answer. It may have gone back to sleep.")

    async def _maybe_write_settings(self, status: BoostStatus) -> None:
        """Set clock, goal and (if changed in Home Assistant) the reminder."""
        bottle_reminder = status.reminder_hex
        if bottle_reminder is not None:
            current = ReminderSettings.from_hex(bottle_reminder)
            self._step(f"reminder on bottle: {current.describe() if current else bottle_reminder}")
        # A reminder chosen in Home Assistant; None means leave the bottle's as it is.
        wanted_reminder = self._reminder_hex or bottle_reminder
        reminder_changed = (
            self._reminder_hex is not None
            and bottle_reminder is not None
            and self._reminder_hex != bottle_reminder
        )

        if not self._update_bottle:
            self._step("bottle updates are off; not setting clock or goal")
            if reminder_changed:
                # A reminder change is an explicit request, so it's sent anyway, on its own.
                await self._send(set_reminder(self._reminder_hex), self._reminder_label())
                self.result.reminder_written = True
                await self._wait_for((Other,), ACK_TIMEOUT_S)
            return
        if wanted_reminder is None:
            self._step("bottle didn't report its reminder settings; not setting clock or goal")
            return
        goal = self._goal_ml if self._goal_ml in GOAL_RANGE_ML else status.goal_ml
        if goal is None:
            return
        now = self._now()
        bottle_time = status.clock.to_naive()
        drift = abs((bottle_time - now).total_seconds()) if bottle_time else None
        if (
            goal == status.goal_ml
            and not reminder_changed
            and drift is not None
            and drift <= CLOCK_TOLERANCE_S
        ):
            self._step(
                f"clock (off by {drift:.0f} s), goal and reminder already right; not rewriting"
            )
            return
        parts = ["set clock"]
        if goal != status.goal_ml:
            parts.append(f"goal {status.goal_ml} → {goal} mL")
        if reminder_changed:
            parts.append(self._reminder_label())
        # One command sets all three, as the official app does on every connect.
        await self._send(sync_settings(goal, now, wanted_reminder), ", ".join(parts))
        self.result.settings_written = True
        self.result.reminder_written = reminder_changed
        if await self._wait_for((SettingsAck,), SETTINGS_TIMEOUT_S) is None:
            # The official app carries on without the acknowledgement too.
            self._step("no acknowledgement for clock/goal/reminder; carrying on")

    async def _maybe_write_light(self) -> None:
        """Send a reminder light change made in Home Assistant (always, as it's explicit)."""
        if self._light is None:
            return
        effect, rgb = self._light
        await self._send(set_reminder_light(effect, rgb), f"reminder light → {effect}, #{rgb}")
        self.result.light_written = True
        # The bottle's reply to this isn't documented; give it a moment either way.
        await self._wait_for((Other,), ACK_TIMEOUT_S)

    def _reminder_label(self) -> str:
        wanted = ReminderSettings.from_hex(self._reminder_hex)
        return f"reminder → {wanted.describe() if wanted else self._reminder_hex}"

    async def _read_logs(self) -> bool:
        """Read stored records into the result. Returns True if the full list arrived."""
        collector = LogCollector()
        packet: Packet | None = None
        for attempt in (1, 2):
            await self._send(
                REQUEST_WATER_LOGS,
                "request stored drinks" + (" (retry)" if attempt > 1 else ""),
            )
            packet = await self._wait_for((NoLogs, LogStart), LOGS_FIRST_TIMEOUT_S)
            if packet is not None:
                break
            self._step("no answer to the stored-drinks request")
        if packet is None:
            raise BottleSyncError(
                "The bottle answered the status request but didn't send its stored "
                "drinks, even when asked twice. Nothing was cleared; it will try again."
            )
        if isinstance(packet, NoLogs):
            self._step("no drinks stored on the bottle")
            return True
        done = collector.add(packet)
        packets = 1
        while not done:
            more = await self._wait_for((LogContinued, LogStart), LOGS_NEXT_TIMEOUT_S)
            if more is None:
                break
            done = collector.add(more)
            packets += 1
        self.result.drinks = collector.drinks
        self.result.records_on_bottle = collector.expected or len(collector.records)
        self._step(
            f"received {len(collector.records)} of {collector.expected} records "
            f"({len(collector.drinks)} drinks) in {packets} "
            f"packet{'s' if packets != 1 else ''}"
        )
        return done

    async def _save_and_clear(self, status: BoostStatus, complete: bool) -> None:
        result = self.result
        result.complete = complete
        # Save first. If this raises, nothing below runs and the bottle keeps everything.
        today_ml = await self._store_drinks(result.drinks)
        if not complete:
            raise BottleSyncError(
                "The bottle stopped sending partway through. The drinks that arrived "
                "were saved; nothing was cleared from the bottle."
            )
        if result.records_on_bottle > 0:
            await self._send(
                clear_records(result.records_on_bottle),
                f"clear {result.records_on_bottle} saved records",
            )
            result.cleared = result.records_on_bottle
        if self._update_bottle and today_ml is not None and today_ml != status.today_ml:
            await self._send(set_today_total(today_ml), f"set bottle's today total to {today_ml} mL")
            result.total_written = today_ml
            if await self._wait_for((Other,), ACK_TIMEOUT_S) is None:
                self._step("no acknowledgement for today's total")
        elif self._update_bottle and today_ml is not None:
            self._step(f"bottle's today total already {today_ml} mL")
        if result.cleared and result.total_written is None:
            # Give the clear command a moment to land before disconnecting.
            await asyncio.sleep(self._delay)
