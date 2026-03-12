"""FTMS integration binary sensor platform."""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FTMS binary sensors."""

    data = entry.runtime_data

    # Only create workout_active sensor when Sole hybrid mode is active
    if data.sole_client is not None:
        async_add_entities([WorkoutActiveSensor(entry=entry)])


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
