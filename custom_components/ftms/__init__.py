"""The FTMS integration."""

import asyncio
import io
import logging
from enum import Enum
from types import MappingProxyType

import pyftms
from bleak import BleakClient
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import close_stale_connections, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_SENSORS,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pyftms.client import client as _pyftms_client
from pyftms.client.const import FEATURE_UUID, FTMS_UUID
from pyftms.client.properties import features as _pyftms_features
from pyftms.client.properties.features import (
    MachineFeatures,
    MachineSettings,
)
from pyftms.serializer import NumSerializer

from .const import CONF_EXTERNAL_HR_ENTITY, DOMAIN
from .coordinator import DataCoordinator
from .models import FtmsData

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

FTMS_SERVICE_UUID = normalize_uuid_str(FTMS_UUID)

type FtmsConfigEntry = ConfigEntry[FtmsData]


# --- Monkey-patch pyftms read_features to handle non-standard feature data ---
_original_read_features = _pyftms_features.read_features


async def _patched_read_features(cli, mt):
    """Patched read_features that handles non-8-byte Feature characteristic data."""
    from pyftms.client.errors import CharacteristicNotFound

    _LOGGER.debug("Reading features and settings (patched)...")

    if (c := cli.services.get_characteristic(FEATURE_UUID)) is None:
        raise CharacteristicNotFound("Machine Feature")

    data = await cli.read_gatt_char(c)
    if len(data) != 8:
        _LOGGER.warning(
            "Feature characteristic returned %d bytes (expected 8), padding/truncating",
            len(data),
        )
        data = (data + b"\x00" * 8)[:8]

    bio, u4 = io.BytesIO(data), NumSerializer("u4")

    features = MachineFeatures(u4.deserialize(bio))
    settings = MachineSettings(u4.deserialize(bio))

    # Delegate the rest (settings filtering + range reading) to original logic
    # by calling the original with the features already parsed.
    # But since original also reads the char, we re-implement the rest here.
    from pyftms.client.properties.features import _read_range
    from pyftms.client.properties.machine_type import MachineType
    from pyftms.client.const import (
        HEART_RATE_RANGE_UUID,
        INCLINATION_RANGE_UUID,
        POWER_RANGE_UUID,
        RESISTANCE_LEVEL_RANGE_UUID,
        SPEED_RANGE_UUID,
        TARGET_HEART_RATE,
        TARGET_INCLINATION,
        TARGET_POWER,
        TARGET_RESISTANCE,
        TARGET_SPEED,
    )

    if MachineType.TREADMILL in mt:
        settings &= ~(MachineSettings.RESISTANCE | MachineSettings.POWER)
    elif MachineType.CROSS_TRAINER in mt:
        settings &= ~(MachineSettings.SPEED | MachineSettings.INCLINE)
    elif MachineType.INDOOR_BIKE in mt:
        settings &= ~(MachineSettings.SPEED | MachineSettings.INCLINE)
    elif MachineType.ROWER in mt:
        settings &= ~(MachineSettings.SPEED | MachineSettings.INCLINE)

    ranges = {}

    if MachineSettings.SPEED in settings:
        if c := cli.services.get_characteristic(SPEED_RANGE_UUID):
            ranges[TARGET_SPEED] = await _read_range(cli, c, "u2.01")
        else:
            settings &= ~MachineSettings.SPEED

    if MachineSettings.INCLINE in settings:
        if c := cli.services.get_characteristic(INCLINATION_RANGE_UUID):
            ranges[TARGET_INCLINATION] = await _read_range(cli, c, "s2.1")
        else:
            settings &= ~MachineSettings.INCLINE

    if MachineSettings.RESISTANCE in settings:
        if c := cli.services.get_characteristic(RESISTANCE_LEVEL_RANGE_UUID):
            ranges[TARGET_RESISTANCE] = await _read_range(cli, c, "s2.1")
        else:
            settings &= ~MachineSettings.RESISTANCE

    if MachineSettings.POWER in settings:
        if c := cli.services.get_characteristic(POWER_RANGE_UUID):
            ranges[TARGET_POWER] = await _read_range(cli, c, "s2")
        else:
            settings &= ~MachineSettings.POWER

    if MachineSettings.HEART_RATE in settings:
        if c := cli.services.get_characteristic(HEART_RATE_RANGE_UUID):
            ranges[TARGET_HEART_RATE] = await _read_range(cli, c, "u1")
        else:
            settings &= ~MachineSettings.HEART_RATE

    _LOGGER.debug("Features: %s", features)
    _LOGGER.debug("Settings: %s", settings)
    _LOGGER.debug("Settings ranges: %s", ranges)

    return features, settings, MappingProxyType(ranges)


# Apply the monkey-patch to both the module and the client that imported it by name
_pyftms_features.read_features = _patched_read_features
_pyftms_client.read_features = _patched_read_features


# --- Monkey-patch realtime data parser to tolerate non-standard packets ---
from pyftms.models.realtime_data.common import RealtimeData as _RealtimeData
from pyftms.client.backends import updater as _pyftms_updater


def _patched_on_notify(self, _c, data):
    """Patched _on_notify that handles non-standard realtime data."""
    _LOGGER.debug("Received notify: %s", data.hex(" ").upper())
    try:
        data_ = self._serializer.deserialize(data)._asdict()
    except Exception as exc:
        _LOGGER.debug("Failed to parse notify data (%s), trying tolerant parse", exc)
        try:
            data_ = _tolerant_deserialize(self._serializer, data)._asdict()
        except Exception as exc2:
            _LOGGER.warning("Could not parse notify data at all: %s", exc2)
            return
    _LOGGER.debug("Received notify dict: %s", data_)
    self._result |= data_

    if data[0] & 1:
        _LOGGER.debug("'More Data' bit is set. Waiting for next data.")
        return

    if any(self._result.values()):
        update = self._result.items() ^ self._prev.items()

        if update := {k: self._result[k] for k, _ in update}:
            _LOGGER.debug("Update data: %s", update)
            from pyftms.client.backends.event import UpdateEvent, UpdateEventData
            from typing import cast
            update = cast(UpdateEventData, update)
            update = UpdateEvent(event_id="update", event_data=update)
            self._cb(update)
            self._prev = self._result.copy()

    self._result.clear()


def _tolerant_deserialize(serializer, data):
    """Parse realtime data tolerantly — ignore trailing bytes and stop on EOF."""
    from pyftms.serializer import get_serializer
    src = io.BytesIO(data)

    # Read the 2-byte flags field
    mask = get_serializer("u2").deserialize(src)
    kwargs = {"mask": mask}
    mask ^= 1  # invert bit 0 (More Data)

    model_cls = serializer._cls
    for field, field_ser in model_cls._iter_fields_serializers():
        if mask & 1:
            try:
                kwargs[field.name] = field_ser.deserialize(src)
            except Exception:
                _LOGGER.debug("EOF/error reading field %s, stopping", field.name)
                break
        mask >>= 1
        if not mask:
            break

    remaining = src.read()
    if remaining:
        _LOGGER.debug("Ignoring %d trailing bytes in notify data", len(remaining))

    return model_cls(**kwargs)


_pyftms_updater.DataUpdater._on_notify = _patched_on_notify
# --- End realtime data monkey-patch ---


# --- Monkey-patch _connect to also discover Sole proprietary service ---
from pyftms.client.client import FitnessMachine as _FitnessMachineClass
from pyftms.client.properties.device_info import DIS_UUID
from .sole_client import SOLE_SERVICE_UUID


async def _patched_connect(self) -> None:
    """Patched _connect that also discovers the Sole proprietary service."""
    if not self._need_connect or self.is_connected:
        return

    await close_stale_connections(self._device)

    _LOGGER.debug("Initialization (patched). Trying to establish connection.")

    self._cli = await establish_connection(
        client_class=BleakClient,
        device=self._device,
        name=self.name,
        disconnected_callback=self._on_disconnect,
        services=[FTMS_UUID, DIS_UUID, SOLE_SERVICE_UUID],
    )

    _LOGGER.debug("Connection success (patched).")

    # Sole F63 only — hardcode features, skip GATT feature read entirely.
    if not hasattr(self, "_device_info"):
        self._device_info = {
            "manufacturer": "Sole Fitness",
            "model": "F63",
        }
    if not hasattr(self, "_m_features"):
        self._m_features = MachineFeatures(0)
        self._m_settings = MachineSettings(0)
        self._settings_ranges = MappingProxyType({})

    # When Sole is present, skip BOTH controller AND updater — pyftms
    # subscriptions interfere with Sole EndWorkout (0x32) detection.
    # FTMS Treadmill Data is subscribed directly via BleakClient instead
    # (exactly like the working ble-test.py hybrid does).
    if self._cli.services.get_service(SOLE_SERVICE_UUID) is not None:
        _LOGGER.warning("Sole service detected — skipping FTMS controller + updater")
    else:
        await self._controller.subscribe(self._cli)
        await self._updater.subscribe(self._cli, self._data_uuid)


_FitnessMachineClass._connect = _patched_connect
# --- End _connect monkey-patch ---


class _HybridState(Enum):
    """State machine for Sole hybrid protocol."""
    FTMS_IDLE = "ftms_idle"          # FTMS subscribed, waiting for speed > 0
    ACTIVATING = "activating"        # Sole subscribe in progress
    SOLE_ACTIVE = "sole_active"      # Sole protocol active, receiving data
    RECONNECTING = "reconnecting"    # BLE disconnect/reconnect in progress


def _get_client_safe(device, advertisement, **kwargs):
    """Create FTMS client, falling back to MachineType.TREADMILL if service data is missing."""
    try:
        return pyftms.get_client(device, advertisement, **kwargs)
    except pyftms.NotFitnessMachineError:
        if FTMS_SERVICE_UUID in advertisement.service_uuids:
            _LOGGER.debug(
                "Creating FTMS client with MachineType.TREADMILL fallback"
            )
            return pyftms.get_client(device, pyftms.MachineType.TREADMILL, **kwargs)
        raise


async def async_unload_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # async_on_unload callbacks (including _cancel_hybrid_tasks) run during
        # async_unload_platforms, so hybrid tasks are already cancelled here.
        if entry.runtime_data.sole_client is not None:
            entry.runtime_data.sole_client.reset()
        if entry.runtime_data.ftms is not None:
            await entry.runtime_data.ftms.disconnect()
            bluetooth.async_rediscover_address(hass, entry.runtime_data.ftms.address)

    return unload_ok


async def async_setup_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Set up device from a config entry."""

    address: str = entry.data[CONF_ADDRESS]

    srv_info = bluetooth.async_last_service_info(hass, address)

    # Hybrid protocol state: None if not Sole, _HybridState enum if Sole detected.
    # Tasks spawned by the hybrid protocol are tracked for cancellation on unload.
    _hybrid_st = None
    _hybrid_tasks: set[asyncio.Task] = set()

    def _track_task(task: asyncio.Task) -> None:
        """Track an async task for cancellation on unload."""
        _hybrid_tasks.add(task)
        task.add_done_callback(_hybrid_tasks.discard)

    def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
        """Disconnect handler. Reload entry on disconnect (unless hybrid reconnecting)."""
        if _hybrid_st == _HybridState.RECONNECTING:
            return
        if ftms_.need_connect:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    # --- Handle offline device gracefully ---
    ftms = None
    if srv_info:
        try:
            ftms = _get_client_safe(
                srv_info.device,
                srv_info.advertisement,
                on_disconnect=_on_disconnect,
            )
        except pyftms.NotFitnessMachineError:
            _LOGGER.warning("Device found but not a valid FTMS device")

    if ftms:
        try:
            await ftms.connect()
        except Exception as exc:
            _LOGGER.warning("FTMS connect failed: %s — entities will show unavailable", exc)
            ftms = None

    coordinator = DataCoordinator(hass, ftms)

    if ftms and ftms.is_connected:
        # Online path — get device info from connected device
        try:
            dev_info = ftms.device_info
        except AttributeError:
            dev_info = {}

        _LOGGER.debug(f"Device Information: {dev_info}")
        _LOGGER.debug(f"Machine type: {ftms.machine_type.name}")

        unique_id = "".join(
            x for x in dev_info.get("serial_number", address) if x.isalnum()
        ).lower()

        device_info_kwargs = {k: v for k, v in dev_info.items() if k in (
            "manufacturer", "model", "sw_version", "hw_version"
        )}

        device_info = dr.DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, unique_id)},
            translation_key=ftms.machine_type.name.lower(),
            **device_info_kwargs,
        )

        sensors = entry.options.get(CONF_SENSORS, [])
        if not sensors:
            sensors = list(ftms.available_properties)
            _LOGGER.info("No sensors configured, using all available: %s", sensors)
    else:
        # Offline path — use static Sole F63 info, entities will show unavailable
        _LOGGER.warning("Device %s not available — setting up entities as unavailable", address)
        from .sole_client import SOLE_SENSORS
        unique_id = "".join(x for x in address if x.isalnum()).lower()
        device_info = dr.DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, unique_id)},
            name="Treadmill",
            manufacturer="Sole Fitness",
            model="F63",
            translation_key="treadmill",
        )
        sensors = entry.options.get(CONF_SENSORS, []) or list(SOLE_SENSORS)

    # --- Sole hybrid protocol support ---
    # State machine: FTMS_IDLE -> ACTIVATING -> SOLE_ACTIVE -> RECONNECTING -> FTMS_IDLE
    #   FTMS_IDLE: direct FTMS Treadmill Data subscription (no pyftms controller/updater)
    #   speed > 0: activate Sole on same connection
    #   EndWorkout (0x32): full BLE disconnect -> reconnect -> FTMS_IDLE
    import struct
    from .sole_client import SoleClient, has_sole_service, SOLE_SENSORS
    from .binary_sensor import WORKOUT_ACTIVE_KEY
    from pyftms.client import const as sole_const
    from pyftms.client.backends.event import UpdateEvent

    FTMS_TREADMILL_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"
    FTMS_CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"

    _RECONNECT_MAX_RETRIES = 3
    _RECONNECT_BASE_DELAY = 2  # seconds
    _SOLE_IDLE_TIMEOUT = 60  # seconds of speed=0 before assuming workout ended

    sole_client = None
    if ftms and hasattr(ftms, '_cli') and ftms.is_connected and has_sole_service(ftms._cli):
        _LOGGER.warning("Sole hybrid mode: direct FTMS subscription (no pyftms updater)")

        _hybrid_st = _HybridState.FTMS_IDLE

        # --- Idle timer: fallback for missing EndWorkout (0x32) ---
        # If speed stays at 0 for _SOLE_IDLE_TIMEOUT seconds while SOLE_ACTIVE,
        # force a reconnect to unblock the START button.
        _idle_timer_unsub = None

        def _start_idle_timer():
            nonlocal _idle_timer_unsub
            if _idle_timer_unsub is not None:
                return  # Timer already running

            @callback
            def _idle_timeout(_now):
                nonlocal _idle_timer_unsub
                _idle_timer_unsub = None
                if _hybrid_st == _HybridState.SOLE_ACTIVE:
                    _LOGGER.warning(
                        "Speed-zero timeout (%ds) — reconnecting to unblock START (workout continues)",
                        _SOLE_IDLE_TIMEOUT,
                    )
                    _track_task(hass.async_create_task(_hybrid_reconnect(is_pause=True)))

            from homeassistant.helpers.event import async_call_later
            _idle_timer_unsub = async_call_later(hass, _SOLE_IDLE_TIMEOUT, _idle_timeout)
            _LOGGER.debug("Idle timer started (%ds)", _SOLE_IDLE_TIMEOUT)

        def _cancel_idle_timer():
            nonlocal _idle_timer_unsub
            if _idle_timer_unsub is not None:
                _idle_timer_unsub()
                _idle_timer_unsub = None

        def _on_sole_event(event):
            # Suppress Sole HR when external HR monitor is configured —
            # the treadmill sends its own (smoothed/delayed) HR via WorkoutData
            # byte 6 which conflicts with the accurate external HR source.
            ext_hr = entry.options.get(CONF_EXTERNAL_HR_ENTITY)
            if ext_hr and sole_const.HEART_RATE in event.event_data:
                del event.event_data[sole_const.HEART_RATE]

            # Track speed for idle timeout (Sole WorkoutData also reports speed)
            if _hybrid_st == _HybridState.SOLE_ACTIVE:
                speed = event.event_data.get(sole_const.SPEED_INSTANT)
                if speed is not None:
                    if speed == 0:
                        _start_idle_timer()
                    else:
                        _cancel_idle_timer()

            if event.event_data:
                coordinator.async_set_updated_data(event)

        async def _subscribe_ftms_direct(cli):
            """Subscribe to FTMS Treadmill Data + send 0xE9 vendor command."""
            ch = cli.services.get_characteristic(FTMS_TREADMILL_DATA_UUID)
            if ch:
                await cli.start_notify(ch, _on_ftms_raw_notify)
                _LOGGER.warning("Subscribed to FTMS Treadmill Data (direct)")

            cp_ch = cli.services.get_characteristic(FTMS_CONTROL_POINT_UUID)
            if cp_ch:
                await cli.start_notify(cp_ch, lambda _c, _d: None)
                await asyncio.sleep(0.3)
                await cli.write_gatt_char(cp_ch, bytes([0x00]), response=True)
                await asyncio.sleep(0.3)
                await cli.write_gatt_char(cp_ch, bytes([0xE9]), response=True)
                _LOGGER.warning("FTMS: sent Request Control + 0xE9")

        async def _activate():
            """Activate Sole protocol on the existing BLE connection."""
            nonlocal _hybrid_st
            if _hybrid_st != _HybridState.ACTIVATING:
                return  # State changed while task was queued
            try:
                await sole_client.subscribe(ftms._cli)
                _hybrid_st = _HybridState.SOLE_ACTIVE
                coordinator.async_set_updated_data(
                    UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: True})
                )
            except Exception:
                _LOGGER.warning("Failed to activate Sole", exc_info=True)
                _hybrid_st = _HybridState.FTMS_IDLE

        async def _hybrid_reconnect(is_pause=False):
            """BLE disconnect + reconnect to exit BLE App mode, with retry."""
            nonlocal _hybrid_st
            _cancel_idle_timer()
            if _hybrid_st == _HybridState.RECONNECTING:
                return  # Already reconnecting (duplicate trigger)
            prev_state = _hybrid_st
            _hybrid_st = _HybridState.RECONNECTING
            _LOGGER.warning(
                "Hybrid reconnect (was %s, pause=%s) -> BLE disconnect/reconnect",
                prev_state.value, is_pause,
            )

            # Let pending writes (especially EndWorkout ACK) flush before
            # tearing down the BLE connection.  Without this delay the ACK
            # task is cancelled by reset() before the GATT write completes.
            await asyncio.sleep(1.0)
            sole_client.reset()

            for attempt in range(_RECONNECT_MAX_RETRIES):
                try:
                    await ftms.disconnect()
                except Exception:
                    _LOGGER.debug("Disconnect error (attempt %d)", attempt + 1, exc_info=True)

                ftms._need_connect = True

                try:
                    await ftms.connect()
                    await _subscribe_ftms_direct(ftms._cli)
                    _LOGGER.warning("Reconnected (attempt %d), START button unblocked", attempt + 1)
                    break
                except Exception:
                    _LOGGER.warning(
                        "Reconnect attempt %d/%d failed",
                        attempt + 1, _RECONNECT_MAX_RETRIES, exc_info=True,
                    )
                    if attempt < _RECONNECT_MAX_RETRIES - 1:
                        await asyncio.sleep(_RECONNECT_BASE_DELAY * (attempt + 1))
            else:
                _LOGGER.error("All reconnect attempts failed, scheduling reload")
                _hybrid_st = _HybridState.FTMS_IDLE
                hass.config_entries.async_schedule_reload(entry.entry_id)
                return

            _hybrid_st = _HybridState.FTMS_IDLE
            if not is_pause:
                coordinator.async_set_updated_data(
                    UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: False})
                )

        def _on_end_workout():
            """Handle Sole EndWorkout — only reconnect from active states."""
            _cancel_idle_timer()
            if _hybrid_st not in (_HybridState.SOLE_ACTIVE, _HybridState.ACTIVATING):
                _LOGGER.warning("EndWorkout received but state=%s, ignoring", _hybrid_st.value if _hybrid_st else None)
                return
            _LOGGER.warning("EndWorkout received in state=%s, triggering reconnect", _hybrid_st.value)
            _track_task(hass.async_create_task(_hybrid_reconnect()))

        sole_client = SoleClient(
            callback=_on_sole_event,
            on_end_workout=_on_end_workout,
        )
        sensors = list(SOLE_SENSORS)

        def _on_ftms_raw_notify(_char, data: bytearray):
            """Parse FTMS Treadmill Data directly — trigger Sole activation on speed > 0."""
            nonlocal _hybrid_st
            if len(data) < 4:
                return
            flags = struct.unpack('<H', data[:2])[0]
            if flags & 0x0001:
                return  # speed not present
            speed = struct.unpack('<H', data[2:4])[0] * 0.01

            update = {sole_const.SPEED_INSTANT: speed}
            coordinator.async_set_updated_data(
                UpdateEvent(event_id="update", event_data=update)
            )

            if _hybrid_st == _HybridState.FTMS_IDLE and speed > 0:
                _hybrid_st = _HybridState.ACTIVATING
                _LOGGER.warning("FTMS speed=%.2f > 0, activating Sole protocol", speed)
                _track_task(hass.async_create_task(_activate()))
            elif _hybrid_st == _HybridState.SOLE_ACTIVE:
                if speed == 0:
                    _start_idle_timer()
                else:
                    _cancel_idle_timer()

        # Register cleanup for hybrid tasks on unload
        @callback
        def _cancel_hybrid_tasks():
            _cancel_idle_timer()
            for task in list(_hybrid_tasks):
                task.cancel()
        entry.async_on_unload(_cancel_hybrid_tasks)

        # Initial subscription after connect
        try:
            await _subscribe_ftms_direct(ftms._cli)
        except Exception as exc:
            await ftms.disconnect()
            raise ConfigEntryNotReady(translation_key="connection_failed") from exc
    # --- End Sole hybrid support ---

    # --- External HR monitor support ---
    ext_hr_entity_id = entry.options.get(CONF_EXTERNAL_HR_ENTITY)
    if ext_hr_entity_id:
        from homeassistant.helpers.event import async_track_state_change_event

        @callback
        def _on_external_hr_change(event):
            """Push external HR monitor value through the coordinator."""
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable", ""):
                return
            try:
                hr_value = int(float(new_state.state))
            except (ValueError, TypeError):
                return
            if hr_value <= 0:
                return
            from pyftms.client import const as hr_const
            from pyftms.client.backends.event import UpdateEvent
            coordinator.async_set_updated_data(
                UpdateEvent(event_id="update", event_data={hr_const.HEART_RATE: hr_value})
            )

        entry.async_on_unload(
            async_track_state_change_event(hass, [ext_hr_entity_id], _on_external_hr_change)
        )
        # Ensure heart_rate is in the sensor list so the entity exists
        from pyftms.client import const as hr_const
        if hr_const.HEART_RATE not in sensors:
            sensors.append(hr_const.HEART_RATE)
        _LOGGER.info("External HR monitor configured: %s", ext_hr_entity_id)
    # --- End external HR support ---

    # Sole is detected either online (sole_client created) or offline (fallback path)
    is_sole = sole_client is not None or ftms is None

    entry.runtime_data = FtmsData(
        entry_id=entry.entry_id,
        unique_id=unique_id,
        device_info=device_info,
        ftms=ftms,
        coordinator=coordinator,
        sensors=sensors,
        is_sole=is_sole,
        sole_client=sole_client,
        external_hr_entity=ext_hr_entity_id,
    )

    @callback
    def _async_on_ble_event(
        srv_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a ble callback. Reload entry when device appears while offline."""
        if ftms is not None:
            ftms.set_ble_device_and_advertisement_data(
                srv_info.device, srv_info.advertisement
            )
        else:
            # Device was offline at setup — reload to establish connection
            _LOGGER.info("Device %s appeared, reloading integration", address)
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_ble_event,
            BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    # Platforms initialization
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_update_handler))

    if ftms is not None:
        async def _async_hass_stop_handler(event: Event) -> None:
            """Close the connection."""
            await ftms.disconnect()

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_hass_stop_handler)
        )

    return True


async def _async_entry_update_handler(
    hass: HomeAssistant, entry: FtmsConfigEntry
) -> None:
    """Options update handler."""

    sensors_changed = entry.options.get(CONF_SENSORS) != entry.runtime_data.sensors
    hr_changed = entry.options.get(CONF_EXTERNAL_HR_ENTITY) != entry.runtime_data.external_hr_entity
    if sensors_changed or hr_changed:
        hass.config_entries.async_schedule_reload(entry.entry_id)
