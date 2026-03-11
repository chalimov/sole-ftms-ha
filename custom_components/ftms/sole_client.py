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

# Sole proprietary BLE UUIDs
SOLE_SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
SOLE_NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
SOLE_WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

# Message framing
_START = 0x5B
_END = 0x5D

# Opcodes
_OP_ACK = 0x00
_OP_SET_WORKOUT_MODE = 0x02
_OP_WORKOUT_MODE = 0x03
_OP_WORKOUT_DATA = 0x06
_OP_USER_PROFILE = 0x07
_OP_PROGRAM = 0x08
_OP_SPEED = 0x11
_OP_INCLINE = 0x12
_OP_HEART_RATE = 0x15
_OP_DEVICE_INFO = 0xF0
_OP_COMMAND = 0xF1

# Workout modes
_WM_IDLE = 0x01
_WM_START = 0x02
_WM_RUNNING = 0x04

# Opcodes that should be acknowledged
_ACK_OPCODES = {
    _OP_WORKOUT_DATA, _OP_SPEED, _OP_INCLINE, _OP_HEART_RATE,
}

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
    """
    result: UpdateEventData = {}

    if len(data) < 14:
        _LOGGER.warning("WorkoutData too short: %d bytes", len(data))
        return result

    minutes = data[0]
    seconds = data[1]
    distance = int.from_bytes(data[2:4], "big")
    calories = int.from_bytes(data[4:6], "big")
    heart_rate = data[6]
    speed_raw = data[7]
    incline = data[8]

    # Elapsed time in seconds (FTMS convention)
    elapsed = minutes * 60 + seconds
    if elapsed > 0:
        result[c.TIME_ELAPSED] = elapsed

    # Distance: 0.01 km units -> meters (FTMS uses meters)
    if distance > 0:
        result[c.DISTANCE_TOTAL] = distance * 10

    # Calories: direct kcal
    if calories > 0:
        result[c.ENERGY_TOTAL] = calories

    # Heart rate: direct bpm
    if heart_rate > 0:
        result[c.HEART_RATE] = heart_rate

    # Speed: raw / 10 = km/h
    speed_kmh = speed_raw / 10.0
    result[c.SPEED_INSTANT] = speed_kmh

    # Incline: direct percentage
    result[c.INCLINATION] = float(incline)

    return result


class SoleClient:
    """Client for the Sole proprietary BLE serial protocol.

    Subscribes to notifications from the Sole service and parses incoming
    workout data, feeding parsed values into the FTMS DataCoordinator.
    """

    def __init__(self, callback: SoleCallback) -> None:
        self._cb = callback
        self._subscribed = False
        self._cli: BleakClient | None = None

    async def subscribe(self, cli: BleakClient) -> None:
        """Subscribe to Sole notifications on an existing BleakClient."""
        if self._subscribed:
            return

        if not has_sole_service(cli):
            _LOGGER.debug("Sole service not found, skipping")
            return

        notify_char = cli.services.get_characteristic(SOLE_NOTIFY_UUID)
        if notify_char is None:
            _LOGGER.warning("Sole notify characteristic not found")
            return

        self._cli = cli
        await cli.start_notify(notify_char, self._on_notify)
        self._subscribed = True
        _LOGGER.info("Subscribed to Sole proprietary notifications")

        # Send init sequence to trigger data flow
        await self._init_handshake()

    def reset(self) -> None:
        """Reset state on disconnect."""
        self._subscribed = False
        self._cli = None

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle incoming Sole notification."""
        _LOGGER.warning("Sole notify: %s", data.hex(" ").upper())

        parsed = _parse_frame(bytes(data))
        if parsed is None:
            return

        opcode, payload = parsed
        update: UpdateEventData = {}

        if opcode == _OP_WORKOUT_DATA:
            update = _parse_workout_data(payload)

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
            _LOGGER.warning("Sole ACK received: %s", payload.hex(" "))

        else:
            _LOGGER.warning("Sole opcode 0x%02X: %s", opcode, payload.hex(" "))

        # Send ACK for data messages
        if opcode in _ACK_OPCODES and self._cli is not None:
            try:
                asyncio.get_event_loop().create_task(self._send_ack(opcode))
            except Exception:
                _LOGGER.debug("Failed to schedule ACK", exc_info=True)

        # Fire update event
        if update:
            event = UpdateEvent(event_id="update", event_data=update)
            self._cb(event)

    async def _init_handshake(self) -> None:
        """Send full init sequence to trigger Sole data flow.

        Follows the treadonme handshake: UserProfile, Program, WorkoutTarget,
        SetWorkoutMode(Start).
        """
        if not self._cli or not self._cli.is_connected:
            return

        try:
            # 1. Send UserProfile: male=0, age=30, weight=155(lbs), height=72(in)
            await self._write(
                _build_frame(_OP_USER_PROFILE, bytes([0x00, 30, 155, 72])),
                "UserProfile",
            )

            # 2. Send Program: Manual mode (0x10, 0x01)
            await self._write(
                _build_frame(_OP_PROGRAM, bytes([0x10, 0x01])),
                "Program(Manual)",
            )

            # 3. Send WorkoutTarget: time=30min, calories=0
            # Format: time_hi, time_lo, cal_hi, cal_lo
            await self._write(
                _build_frame(0x04, bytes([0x00, 30, 0x00, 0x00])),
                "WorkoutTarget(30min)",
            )

            # 4. Send SetWorkoutMode: Start (0x02)
            await self._write(
                _build_frame(_OP_SET_WORKOUT_MODE, bytes([_WM_START])),
                "SetWorkoutMode(Start)",
            )

        except Exception:
            _LOGGER.warning("Sole init handshake failed", exc_info=True)

    async def _write(self, data: bytes, label: str = "") -> None:
        """Write data to the Sole write characteristic with delay."""
        if self._cli and self._cli.is_connected:
            await self._cli.write_gatt_char(SOLE_WRITE_UUID, data, response=False)
            _LOGGER.warning("Sent Sole %s: %s", label, data.hex(" ").upper())
            await asyncio.sleep(0.5)

    async def _send_ack(self, opcode: int) -> None:
        """Send ACK to the treadmill."""
        try:
            if self._cli and self._cli.is_connected:
                ack = _build_ack(opcode)
                await self._cli.write_gatt_char(SOLE_WRITE_UUID, ack, response=False)
                _LOGGER.debug("Sent Sole ACK for 0x%02X", opcode)
        except Exception:
            _LOGGER.debug("Failed to send Sole ACK", exc_info=True)
