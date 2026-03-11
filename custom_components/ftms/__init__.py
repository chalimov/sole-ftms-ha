"""The FTMS integration."""

import io
import logging
from types import MappingProxyType

import pyftms
from bleak import BleakClient
from bleak.exc import BleakError
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
    SettingRange,
)
from pyftms.serializer import NumSerializer

from .const import DOMAIN
from .coordinator import DataCoordinator
from .models import FtmsData

PLATFORMS: list[Platform] = [
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
    except (AssertionError, EOFError, Exception) as exc:
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
            except (EOFError, Exception):
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

    if not hasattr(self, "_device_info"):
        from pyftms.client.properties import read_device_info
        self._device_info = await read_device_info(self._cli)

    if not hasattr(self, "_features"):
        self._m_features, self._m_settings, self._settings_ranges = (
            await _patched_read_features(self._cli, self._machine_type)
        )

    await self._controller.subscribe(self._cli)
    await self._updater.subscribe(self._cli, self._data_uuid)


_FitnessMachineClass._connect = _patched_connect
# --- End _connect monkey-patch ---


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
        if entry.runtime_data.sole_client is not None:
            entry.runtime_data.sole_client.reset()
        await entry.runtime_data.ftms.disconnect()
        bluetooth.async_rediscover_address(hass, entry.runtime_data.ftms.address)

    return unload_ok


async def async_setup_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Set up device from a config entry."""

    address: str = entry.data[CONF_ADDRESS]

    if not (srv_info := bluetooth.async_last_service_info(hass, address)):
        raise ConfigEntryNotReady(translation_key="device_not_found")

    def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
        """Disconnect handler. Reload entry on disconnect."""

        if ftms_.need_connect:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    try:
        ftms = _get_client_safe(
            srv_info.device,
            srv_info.advertisement,
            on_disconnect=_on_disconnect,
        )

    except pyftms.NotFitnessMachineError:
        raise ConfigEntryNotReady(translation_key="ftms_error")

    coordinator = DataCoordinator(hass, ftms)

    try:
        await ftms.connect()

    except (BleakError, AssertionError, Exception) as exc:
        _LOGGER.warning("FTMS connect failed: %s", exc)
        raise ConfigEntryNotReady(translation_key="connection_failed") from exc

    assert ftms.machine_type.name

    try:
        dev_info = ftms.device_info
    except AttributeError:
        dev_info = {}

    _LOGGER.debug(f"Device Information: {dev_info}")
    _LOGGER.debug(f"Machine type: {ftms.machine_type.name}")
    _LOGGER.debug(f"Available sensors: {ftms.available_properties}")
    try:
        _LOGGER.debug(f"Supported settings: {ftms.supported_settings}")
        _LOGGER.debug(f"Supported ranges: {ftms.supported_ranges}")
    except AttributeError:
        _LOGGER.debug("Supported settings/ranges not available")

    unique_id = "".join(
        x for x in dev_info.get("serial_number", address) if x.isalnum()
    ).lower()

    _LOGGER.debug(f"Registered new FTMS device. UniqueID is '{unique_id}'.")

    device_info_kwargs = {k: v for k, v in dev_info.items() if k in (
        "manufacturer", "model", "sw_version", "hw_version"
    )}

    device_info = dr.DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, ftms.address)},
        identifiers={(DOMAIN, unique_id)},
        translation_key=ftms.machine_type.name.lower(),
        **device_info_kwargs,
    )

    sensors = entry.options.get(CONF_SENSORS, [])
    if not sensors:
        sensors = list(ftms.available_properties)
        _LOGGER.info("No sensors configured, using all available: %s", sensors)

    # --- Sole proprietary protocol support ---
    from .sole_client import SoleClient, has_sole_service, SOLE_SENSORS
    from pyftms.client import const as _ftms_const

    sole_client = None
    if hasattr(ftms, '_cli') and ftms.is_connected and has_sole_service(ftms._cli):
        _LOGGER.info("Sole proprietary service detected, subscribing")

        def _on_sole_event(event):
            coordinator.async_set_updated_data(event)

        sole_client = SoleClient(callback=_on_sole_event)
        try:
            await sole_client.subscribe(ftms._cli)
        except Exception:
            _LOGGER.warning("Failed to subscribe to Sole service", exc_info=True)
            sole_client = None

        if sole_client is not None:
            # Override sensor list — only keep sensors that actually provide data
            sensors = list(SOLE_SENSORS)

            # Watch FTMS updates for speed > 0 to activate Sole protocol
            _orig_ftms_cb = ftms._callback

            def _ftms_cb_with_sole_trigger(data):
                if _orig_ftms_cb:
                    _orig_ftms_cb(data)
                # Check if FTMS reports speed > 0 (workout started)
                if hasattr(data, 'event_data'):
                    speed = data.event_data.get(_ftms_const.SPEED_INSTANT, 0)
                elif isinstance(data, dict):
                    speed = data.get(_ftms_const.SPEED_INSTANT, 0)
                else:
                    speed = 0
                if speed and speed > 0 and not sole_client._activated:
                    asyncio.ensure_future(sole_client.activate())

            ftms.set_callback(_ftms_cb_with_sole_trigger)
    # --- End Sole support ---

    entry.runtime_data = FtmsData(
        entry_id=entry.entry_id,
        unique_id=unique_id,
        device_info=device_info,
        ftms=ftms,
        coordinator=coordinator,
        sensors=sensors,
        sole_client=sole_client,
    )

    @callback
    def _async_on_ble_event(
        srv_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a ble callback."""

        ftms.set_ble_device_and_advertisement_data(
            srv_info.device, srv_info.advertisement
        )

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

    if entry.options[CONF_SENSORS] != entry.runtime_data.sensors:
        hass.config_entries.async_schedule_reload(entry.entry_id)
