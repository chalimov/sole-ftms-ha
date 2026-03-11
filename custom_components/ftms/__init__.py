"""The FTMS integration."""

import io
import logging
from types import MappingProxyType

import pyftms
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
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
# --- End monkey-patch ---


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

    entry.runtime_data = FtmsData(
        entry_id=entry.entry_id,
        unique_id=unique_id,
        device_info=device_info,
        ftms=ftms,
        coordinator=coordinator,
        sensors=entry.options[CONF_SENSORS],
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
