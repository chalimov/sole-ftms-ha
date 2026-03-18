"""FTMS integration number platform."""

import dataclasses as dc
import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.const import UnitOfPower, UnitOfSpeed
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyftms.client import const as c

from . import FtmsConfigEntry
from .entity import FtmsEntity

_LOGGER = logging.getLogger(__name__)

_NUMBERS_SENSORS_MAP = {
    c.TARGET_SPEED: c.SPEED_INSTANT,
    c.TARGET_INCLINATION: c.INCLINATION,
    c.TARGET_RESISTANCE: c.RESISTANCE_LEVEL,
    c.TARGET_POWER: c.POWER_INSTANT,
}

_SPEED = NumberEntityDescription(
    key=c.TARGET_SPEED,
    device_class=NumberDeviceClass.SPEED,
    native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
)

_INCLINATION = NumberEntityDescription(
    key=c.TARGET_INCLINATION,
    native_unit_of_measurement="%",
)

_RESISTANCE_LEVEL = NumberEntityDescription(
    key=c.TARGET_RESISTANCE,
)

_POWER = NumberEntityDescription(
    key=c.TARGET_POWER,
    device_class=NumberDeviceClass.POWER,
    native_unit_of_measurement=UnitOfPower.WATT,
)

_ENTITIES = (
    _RESISTANCE_LEVEL,
    _POWER,
    _SPEED,
    _INCLINATION,
)

# Sole-specific: incline via FTMS CP (absolute, 0-15%, step 1)
SOLE_INCLINE_KEY = "sole_target_incline"

_SOLE_INCLINATION = NumberEntityDescription(
    key=SOLE_INCLINE_KEY,
    name="Target incline",
    native_unit_of_measurement="%",
    native_min_value=0,
    native_max_value=15,
    native_step=1,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a FTMS number entry."""

    data = entry.runtime_data
    entities = []

    if data.is_sole:
        # Sole mode: add Sole incline number entity
        entities.append(SoleInclineEntity(entry=entry))
    elif data.ftms is not None:
        # Standard FTMS number entities
        ranges_ = data.ftms.supported_ranges
        for desc in _ENTITIES:
            if range_ := ranges_.get(desc.key):
                entities.append(
                    FtmsNumberEntity(
                        entry=entry,
                        description=dc.replace(
                            desc,
                            native_min_value=range_.min_value,
                            native_max_value=range_.max_value,
                            native_step=range_.step,
                        ),
                    )
                )

    async_add_entities(entities)


class FtmsNumberEntity(FtmsEntity, NumberEntity):
    """Representation of FTMS numbers."""

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value from HA."""

        await self.ftms.set_setting(self.key, value)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        e, key = self.coordinator.data, self.key

        if e.event_id == "update":
            if (key := _NUMBERS_SENSORS_MAP.get(key)) is None:
                return

        elif e.event_id != "setup":
            return

        if (value := e.event_data.get(key)) is not None:
            self._attr_native_value = value
            self.async_write_ha_state()


class SoleInclineEntity(FtmsEntity, NumberEntity):
    """Sole incline control via FTMS Control Point (absolute, no beep)."""

    def __init__(self, entry: FtmsConfigEntry) -> None:
        super().__init__(entry, _SOLE_INCLINATION)

    async def async_set_native_value(self, value: float) -> None:
        """Set incline via FTMS CP opcode 0x03."""
        sole = self._data.sole_client
        if sole is None:
            _LOGGER.warning("Sole client not available for incline control")
            return
        await sole.set_incline(value)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update from Sole WorkoutData incline."""
        e = self.coordinator.data
        if e.event_id == "update" and c.INCLINATION in e.event_data:
            self._attr_native_value = e.event_data[c.INCLINATION]
            self.async_write_ha_state()
