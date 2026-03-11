"""Sole proprietary BLE protocol client.

Connects to the Sole Serial service (UUID 49535343-FE7D-4AE5-8FA9-9FAFD205E455)
via an existing BleakClient and parses WorkoutData messages to supplement FTMS
data with incline, distance, calories, and other fields.

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

# Opcodes that get a standard ACK (opcode 0x00 + echoed opcode + OK)
_STANDARD_ACK_OPCODES = {
    _OP_WORKOUT_DATA, _OP_HR_TYPE, _OP_ERROR_CODE,
    _OP_SPEED, _OP_INCLINE, _OP_LEVEL, _OP_RPM, _OP_HEART_RATE,
    _OP_TARGET_HR, _OP_MAX_SPEED, _OP_MAX_INCLINE, _OP_MAX_LEVEL,
    _OP_END_WORKOUT, _OP_PROGRAM_GFX,
}

# WorkoutMode (0x03) is special: must ECHO back the same message, not ACK

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

    # Speed: raw / 10 = km/h
    result[c.SPEED_INSTANT] = speed_raw / 10.0

    # Incline: direct percentage
    result[c.INCLINATION] = float(incline)

    return result


class SoleClient:
    """Client for the Sole proprietary BLE serial protocol."""

    def __init__(self, callback: SoleCallback) -> None:
        self._cb = callback
        self._subscribed = False
        self._cli: BleakClient | None = None
        self._data_started = False  # True once Command(Start) sent

    async def subscribe(self, cli: BleakClient) -> None:
        """Subscribe to Sole notifications on an existing BleakClient."""
        if self._subscribed:
            return

        if not has_sole_service(cli):
            _LOGGER.debug("Sole service not found, skipping")
            return

        self._cli = cli

        # Subscribe to BOTH notify characteristics
        for uuid, label in [
            (SOLE_NOTIFY_UUID, "RX"),
            (SOLE_NOTIFY2_UUID, "2nd"),
        ]:
            char = cli.services.get_characteristic(uuid)
            if char:
                await cli.start_notify(char, self._on_notify)
                _LOGGER.info("Subscribed to Sole %s notify", label)

        self._subscribed = True

        # Initialize protocol and trigger data streaming
        await self._start_workout()

    def reset(self) -> None:
        """Reset state on disconnect."""
        self._subscribed = False
        self._cli = None
        self._data_started = False

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle incoming Sole notification."""
        parsed = _parse_frame(bytes(data))
        if parsed is None:
            _LOGGER.debug("Sole unparseable: %s", data.hex(" "))
            return

        opcode, payload = parsed
        update: UpdateEventData = {}

        if opcode == _OP_WORKOUT_DATA:
            update = _parse_workout_data(payload)
            _LOGGER.debug("Sole WorkoutData: %s", update)

        elif opcode == _OP_INCLINE and len(payload) >= 1:
            update[c.INCLINATION] = float(payload[0])

        elif opcode == _OP_SPEED and len(payload) >= 1:
            update[c.SPEED_INSTANT] = payload[0] / 10.0

        elif opcode == _OP_HEART_RATE and len(payload) >= 1:
            if payload[0] > 0:
                update[c.HEART_RATE] = payload[0]

        elif opcode == _OP_DEVICE_INFO:
            _LOGGER.info("Sole DeviceInfo: %s", payload.hex(" "))

        elif opcode == _OP_ACK:
            _LOGGER.debug("Sole ACK: %s", payload.hex(" "))

        # WorkoutMode (0x03) is SPECIAL: echo it back (not standard ACK)
        if opcode == _OP_WORKOUT_MODE and self._cli is not None:
            asyncio.ensure_future(self._echo_raw(bytes(data)))
            # If workout just started (mode != Idle/0x01), trigger data streaming
            if not self._data_started and len(payload) >= 1 and payload[0] != 0x01:
                self._data_started = True
                asyncio.ensure_future(self._trigger_data_stream())

        # Standard ACK for data messages
        elif opcode in _STANDARD_ACK_OPCODES and self._cli is not None:
            asyncio.ensure_future(self._send_ack(opcode))

        # EndWorkout (0x32) — reset state and zero out sensors
        if opcode == _OP_END_WORKOUT:
            self._data_started = False
            update = {
                c.SPEED_INSTANT: 0.0,
                c.INCLINATION: 0.0,
                c.HEART_RATE: 0,
            }

        # Fire update event
        if update:
            event = UpdateEvent(event_id="update", event_data=update)
            self._cb(event)

    async def _start_workout(self) -> None:
        """Initialize Sole protocol — handshake only.

        Sends DeviceInfo to complete the handshake. Command(Start) is deferred
        until we detect the user has started a workout (WorkoutMode != Idle).
        """
        if not self._cli or not self._cli.is_connected:
            return

        try:
            # Request device info (triggers WorkoutMode exchange)
            await self._write(_build_frame(_OP_DEVICE_INFO, b""))
        except Exception:
            _LOGGER.warning("Sole handshake failed", exc_info=True)

    async def _trigger_data_stream(self) -> None:
        """Send Command(Start) to trigger WorkoutData streaming.

        Called when we detect the user started a workout on the treadmill.
        """
        if not self._cli or not self._cli.is_connected:
            return

        try:
            _LOGGER.info("Sole: workout detected, triggering data stream")
            await asyncio.sleep(1.0)
            await self._write(_build_frame(_OP_COMMAND, bytes([0x01])))
        except Exception:
            _LOGGER.warning("Sole trigger data stream failed", exc_info=True)

    async def _write(self, data: bytes) -> None:
        """Write data to the Sole write characteristic."""
        if self._cli and self._cli.is_connected:
            _LOGGER.debug("Sole TX: %s", data.hex(" "))
            await self._cli.write_gatt_char(SOLE_WRITE_UUID, data, response=False)
            await asyncio.sleep(0.3)

    async def _echo_raw(self, data: bytes) -> None:
        """Echo raw frame back to the treadmill (for WorkoutMode)."""
        try:
            if self._cli and self._cli.is_connected:
                await self._cli.write_gatt_char(SOLE_WRITE_UUID, data, response=False)
        except Exception:
            _LOGGER.debug("Failed to echo WorkoutMode", exc_info=True)

    async def _send_ack(self, opcode: int) -> None:
        """Send standard ACK to the treadmill."""
        try:
            if self._cli and self._cli.is_connected:
                ack = _build_ack(opcode)
                await self._cli.write_gatt_char(SOLE_WRITE_UUID, ack, response=False)
        except Exception:
            _LOGGER.debug("Failed to send Sole ACK for 0x%02X", opcode, exc_info=True)
