"""FTMS integration binary sensor platform."""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FtmsConfigEntry
from .entity import FtmsEntity

_LOGGER = logging.getLogger(__name__)

WORKOUT_ACTIVE_KEY = "workout_active"

_WORKOUT_ACTIVE = BinarySensorEntityDescription(
    key=WORKOUT_ACTIVE_KEY,
    name="Workout active",
)

_CONNECTION_STATUS = BinarySensorEntityDescription(
    key="connected",
    name="Connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FTMS binary sensors."""

    data = entry.runtime_data
    entities = [ConnectionStatusSensor(entry=entry)]

    # Only create workout_active sensor when Sole hybrid mode is active
    if data.sole_client is not None:
        entities.append(WorkoutActiveSensor(entry=entry))

    async_add_entities(entities)


class ConnectionStatusSensor(FtmsEntity, BinarySensorEntity):
    """Binary sensor showing BLE connection status (always available)."""

    def __init__(self, entry: FtmsConfigEntry) -> None:
        super().__init__(entry, _CONNECTION_STATUS)
        self._attr_is_on = (
            self._data.ftms is not None and self._data.ftms.is_connected
        )

    @property
    def available(self) -> bool:
        """Always available — shows disconnected when treadmill is off."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        is_on = self._data.ftms is not None and self._data.ftms.is_connected
        if self._attr_is_on != is_on:
            self._attr_is_on = is_on
            self.async_write_ha_state()


class WorkoutActiveSensor(FtmsEntity, BinarySensorEntity):
    """Binary sensor that indicates whether a workout is currently active."""

    def __init__(self, entry: FtmsConfigEntry) -> None:
        super().__init__(entry, _WORKOUT_ACTIVE)
        self._attr_is_on = False

    @callback
    def _handle_coordinator_update(self) -> None:
        e = self.coordinator.data

        if e.event_id == "update" and WORKOUT_ACTIVE_KEY in e.event_data:
            self._attr_is_on = bool(e.event_data[WORKOUT_ACTIVE_KEY])
            self.async_write_ha_state()
