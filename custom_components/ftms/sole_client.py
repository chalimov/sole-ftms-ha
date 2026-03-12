"""Sole proprietary BLE protocol client.

Connects to the Sole Serial service (UUID 49535343-FE7D-4AE5-8FA9-9FAFD205E455)
via an existing BleakClient and parses WorkoutData messages to supplement FTMS
data with incline, distance, calories, and other fields.

Protocol: selective ACK mode. We echo WorkoutMode and ACK standard opcodes
(WorkoutData, Speed, Incline, etc.) but NEVER respond to ErrorCode (0x10),
which is the idle heartbeat that triggers "BLE App" mode and blocks buttons.
We never send Command/UserProfile/Program/SetWorkoutMode — the user controls
the treadmill from its physical console.

Protocol reference: github.com/swedishborgie/treadonme
"""

import asyncio
import logging
from typing import Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from pyftms.client import const as c
from pyftms.client.backends.event import UpdateEvent, UpdateEventData

_LOGGER = logging.getLogger(__name__)

# Dedicated file logger so we can read the full trace (HA log API only returns 100 lines)
_FILE_LOGGER = logging.getLogger("sole_debug_file")
_FILE_LOGGER.setLevel(logging.DEBUG)
_FILE_LOGGER.propagate = False
try:
    import os
    os.makedirs("/config/www", exist_ok=True)
    _fh = logging.FileHandler("/config/www/sole_debug.log", mode="w")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _FILE_LOGGER.addHandler(_fh)
except Exception:
    pass  # not running on HA, ignore


def _log(msg, *args):
    """Log to both HA log (WARNING) and dedicated file (DEBUG)."""
    _LOGGER.warning(msg, *args)
    _FILE_LOGGER.debug(msg, *args)

# Sole proprietary BLE UUIDs (Microchip Transparent UART)
SOLE_SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
SOLE_NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"   # RX (main notify)
SOLE_NOTIFY2_UUID = "49535343-4c8a-39b3-2f49-511cff073b7e"  # 2nd notify char
SOLE_WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"     # TX (write)

# Message framing
_START = 0x5B
_END = 0x5D

# Opcodes (from treadonme constants.go)
_OP_ACK = 0x00
_OP_SET_WORKOUT_MODE = 0x02
_OP_WORKOUT_MODE = 0x03
_OP_WORKOUT_TARGET = 0x04
_OP_WORKOUT_DATA = 0x06
_OP_USER_PROFILE = 0x07
_OP_PROGRAM = 0x08
_OP_HR_TYPE = 0x09
_OP_ERROR_CODE = 0x10
_OP_SPEED = 0x11
_OP_INCLINE = 0x12
_OP_LEVEL = 0x13
_OP_RPM = 0x14
_OP_HEART_RATE = 0x15
_OP_TARGET_HR = 0x20
_OP_MAX_SPEED = 0x21
_OP_MAX_INCLINE = 0x22
_OP_MAX_LEVEL = 0x23
_OP_USER_INCLINE = 0x25
_OP_USER_LEVEL = 0x27
_OP_END_WORKOUT = 0x32
_OP_PROGRAM_GFX = 0x40
_OP_DEVICE_INFO = 0xF0
_OP_COMMAND = 0xF1

# Opcodes that get a standard ACK (echo opcode + "OK")
_STANDARD_ACK_OPCODES = {
    _OP_WORKOUT_DATA, _OP_HR_TYPE, _OP_ERROR_CODE,
    _OP_SPEED, _OP_INCLINE, _OP_LEVEL, _OP_RPM, _OP_HEART_RATE,
    _OP_TARGET_HR, _OP_MAX_SPEED, _OP_MAX_INCLINE, _OP_MAX_LEVEL,
    _OP_END_WORKOUT, _OP_PROGRAM_GFX,
}

# Opcodes that get NO response (just read the data)
_NO_RESPONSE_OPCODES = {_OP_ACK, _OP_SET_WORKOUT_MODE, _OP_DEVICE_INFO}

# Sensors provided by the Sole protocol
SOLE_SENSORS = [
    c.INCLINATION,
    c.DISTANCE_TOTAL,
    c.ENERGY_TOTAL,
    c.TIME_REMAINING,
    c.HEART_RATE,
    c.SPEED_INSTANT,
]

type SoleCallback = Callable[[UpdateEvent], None]


def has_sole_service(cli: BleakClient) -> bool:
    """Check if the BleakClient has discovered the Sole proprietary service."""
    return cli.services.get_service(SOLE_SERVICE_UUID) is not None


def _build_frame(opcode: int, data: bytes = b"") -> bytes:
    """Build a framed Sole message."""
    length = 1 + len(data)  # opcode + data
    return bytes([_START, length, opcode]) + data + bytes([_END])


def _build_ack(opcode: int) -> bytes:
    """Build an ACK frame for a received opcode."""
    return _build_frame(_OP_ACK, bytes([opcode, 0x4F, 0x4B]))


def _parse_frame(raw: bytes) -> tuple[int, bytes] | None:
    """Extract opcode and data from a framed Sole message.

    Frame: [0x5B] [length] [opcode] [data...] [0x5D]
    Returns (opcode, data_bytes) or None if invalid.
    """
    if len(raw) < 4 or raw[0] != _START or raw[-1] != _END:
        return None
    opcode = raw[2]
    data = raw[3:-1]
    return opcode, data


def _parse_workout_data(data: bytes) -> UpdateEventData:
    """Parse WorkoutData payload (14 bytes after opcode).

    Layout: minute(1) + second(1) + distance(2 BE) + calories(2 BE) +
            heartrate(1) + speed(1, /10) + incline(1) + hrtype(1) +
            intervaltime(1) + recoverytime(1) + programrow(1) + programcolumn(1)

    Note: The minute/second fields count DOWN (time remaining), not up.
    """
    result: UpdateEventData = {}

    if len(data) < 14:
        _LOGGER.debug("WorkoutData too short: %d bytes", len(data))
        return result

    minutes = data[0]
    seconds = data[1]
    distance = int.from_bytes(data[2:4], "big")
    calories = int.from_bytes(data[4:6], "big")
    heart_rate = data[6]
    speed_raw = data[7]
    incline = data[8]

    # Time remaining in seconds (Sole counts down from workout target)
    time_remaining = minutes * 60 + seconds
    result[c.TIME_REMAINING] = time_remaining

    # Distance: raw units (0.01 km) -> meters (FTMS uses meters)
    if distance > 0:
        result[c.DISTANCE_TOTAL] = distance * 10

    # Calories: direct kcal
    if calories > 0:
        result[c.ENERGY_TOTAL] = calories

    # Heart rate: direct bpm
    if heart_rate > 0:
        result[c.HEART_RATE] = heart_rate

    # Speed: raw / 10 = km/h (FTMS also provides speed, including speed=0 for paused)
    result[c.SPEED_INSTANT] = speed_raw / 10.0

    # Incline: direct percentage
    result[c.INCLINATION] = float(incline)

    return result


class SoleClient:
    """Client for the Sole proprietary BLE serial protocol.

    Passive telemetry mode:
    1. subscribe() — subscribe to notifications, send GetDeviceInfo to establish
       communication, then always echo WorkoutMode and ACK standard opcodes.
    2. Never sends Command/UserProfile/Program/SetWorkoutMode — user controls
       the treadmill from its physical console.
    3. Buttons work at all times (before, during, and after workouts).
    """

    def __init__(
        self,
        callback: SoleCallback,
        on_end_workout: Callable[[], None] | None = None,
    ) -> None:
        self._cb = callback
        self._on_end_workout = on_end_workout
        self._subscribed = False
        self._cli: BleakClient | None = None
        # Must hold strong references to tasks to prevent GC before completion.
        # See: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._pending_writes: set[asyncio.Task] = set()

    async def subscribe(self, cli: BleakClient) -> None:
        """Subscribe to Sole notifications and send GetDeviceInfo."""
        if self._subscribed:
            return

        if not has_sole_service(cli):
            _LOGGER.debug("Sole service not found, skipping")
            return

        self._cli = cli

        for uuid, label in [
            (SOLE_NOTIFY_UUID, "RX"),
            (SOLE_NOTIFY2_UUID, "2nd"),
        ]:
            char = cli.services.get_characteristic(uuid)
            if char:
                await cli.start_notify(char, self._on_notify)
                _LOGGER.warning("Subscribed to Sole %s notify", label)

        self._subscribed = True

        # Send GetDeviceInfo to kick-start communication.
        # Without this, the treadmill ignores us entirely.
        try:
            get_info = _build_frame(_OP_DEVICE_INFO)
            await cli.write_gatt_char(SOLE_WRITE_UUID, get_info, response=False)
            _LOGGER.warning("Sole: sent GetDeviceInfo to initiate data flow")
        except Exception:
            _LOGGER.warning("Sole: failed to send GetDeviceInfo", exc_info=True)

        _LOGGER.warning("Sole: subscribed (selective ACK — skip ErrorCode to keep buttons free)")

    def reset(self) -> None:
        """Reset state on disconnect."""
        self._subscribed = False
        self._cli = None

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle incoming Sole notification.

        Selective ACK strategy:
        - ErrorCode (0x10): NEVER respond — idle heartbeat that triggers button lock
        - WorkoutMode (0x03): echo the exact message back
        - Standard opcodes: send ACK
        - ACK/SetWorkoutMode/DeviceInfo: no response needed
        """
        _log("Sole RX: %s", data.hex(" "))

        parsed = _parse_frame(bytes(data))
        if parsed is None:
            _log("Sole unparseable frame: %s", data.hex(" "))
            return

        opcode, payload = parsed
        _log("Sole parsed: op=0x%02X payload=%s", opcode, payload.hex(" ") if payload else "(empty)")
        update: UpdateEventData = {}

        # --- Parse telemetry data ---
        if opcode == _OP_WORKOUT_DATA:
            update = _parse_workout_data(payload)
            _log("Sole WorkoutData: %s", update)

        elif opcode == _OP_WORKOUT_MODE:
            mode_val = payload[0] if payload else -1
            _log("Sole WorkoutMode: value=0x%02X (%d)", mode_val, mode_val)

        elif opcode == _OP_INCLINE and len(payload) >= 1:
            update[c.INCLINATION] = float(payload[0])
            _log("Sole Incline: %s", payload[0])

        elif opcode == _OP_SPEED and len(payload) >= 1:
            update[c.SPEED_INSTANT] = payload[0] / 10.0
            _log("Sole Speed: %s km/h", payload[0] / 10.0)

        elif opcode == _OP_HEART_RATE and len(payload) >= 1:
            if payload[0] > 0:
                update[c.HEART_RATE] = payload[0]
            _log("Sole HeartRate: %s", payload[0])

        elif opcode == _OP_DEVICE_INFO:
            _log("Sole DeviceInfo response: %s", payload.hex(" "))

        elif opcode == _OP_END_WORKOUT:
            _log("Sole EndWorkout received, payload: %s", payload.hex(" ") if payload else "(empty)")
            if self._on_end_workout:
                self._on_end_workout()

        elif opcode == _OP_ACK:
            _log("Sole ACK received: %s", payload.hex(" ") if payload else "(empty)")

        elif opcode == _OP_WORKOUT_TARGET:
            _log("Sole WorkoutTarget: %s", payload.hex(" ") if payload else "(empty)")

        elif opcode == _OP_USER_PROFILE:
            _log("Sole UserProfile: %s", payload.hex(" ") if payload else "(empty)")

        elif opcode == _OP_PROGRAM:
            _log("Sole Program: %s", payload.hex(" ") if payload else "(empty)")

        else:
            _log("Sole opcode 0x%02X: %s", opcode, payload.hex(" ") if payload else "(empty)")

        # --- Selective ACK: respond to everything EXCEPT ErrorCode (0x10) ---
        # ErrorCode 0x10 is the idle heartbeat. ACKing it puts the treadmill in
        # "BLE App" mode which blocks physical buttons. Skipping it keeps buttons
        # free. We still ACK workout data and echo WorkoutMode so data flows.
        if opcode == _OP_ERROR_CODE:
            _log("Sole: SKIP response for ErrorCode 0x%02X (avoid button block)", opcode)
        elif opcode == _OP_WORKOUT_MODE:
            self._schedule_write(self._echo_raw(bytes(data)))
            _log("Sole: echoed WorkoutMode")
        elif opcode in _STANDARD_ACK_OPCODES:
            self._schedule_write(self._send_ack(opcode))
            _log("Sole: sent ACK for 0x%02X", opcode)
        elif opcode in _NO_RESPONSE_OPCODES:
            _log("Sole: no response needed for 0x%02X", opcode)
        else:
            _log("Sole: no handler for 0x%02X, skipping", opcode)

        # --- Fire update event ---
        if update:
            event = UpdateEvent(event_id="update", event_data=update)
            self._cb(event)

    def _schedule_write(self, coro) -> None:
        """Schedule a coroutine as a task with a strong reference to prevent GC."""
        task = asyncio.ensure_future(coro)
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def _echo_raw(self, data: bytes) -> None:
        """Echo raw frame back to the treadmill (for WorkoutMode)."""
        try:
            if self._cli and self._cli.is_connected:
                await self._cli.write_gatt_char(SOLE_WRITE_UUID, data, response=False)
                _log("Sole TX sent: %s", data.hex(" "))
            else:
                _log("Sole TX: client not connected, cannot echo")
        except Exception:
            _LOGGER.warning("Failed to echo WorkoutMode", exc_info=True)

    async def _send_ack(self, opcode: int) -> None:
        """Send standard ACK to the treadmill."""
        try:
            if self._cli and self._cli.is_connected:
                ack = _build_ack(opcode)
                await self._cli.write_gatt_char(SOLE_WRITE_UUID, ack, response=False)
                _log("Sole TX sent: %s (ACK 0x%02X)", ack.hex(" "), opcode)
            else:
                _log("Sole TX: client not connected, cannot ACK 0x%02X", opcode)
        except Exception:
            _LOGGER.warning("Failed to send Sole ACK for 0x%02X", opcode, exc_info=True)
