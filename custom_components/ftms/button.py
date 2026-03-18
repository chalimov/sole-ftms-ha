"""FTMS integration button platform."""

import logging
from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyftms.client import const as c

from . import FtmsConfigEntry
from .entity import FtmsEntity

_LOGGER = logging.getLogger(__name__)

_ENTITIES = (
    c.RESET,
    c.STOP,
    c.START,
    c.PAUSE,
)

# Sole-specific button keys
SOLE_SPEED_UP = "sole_speed_up"
SOLE_SPEED_DOWN = "sole_speed_down"
SOLE_STOP = "sole_stop"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a FTMS button entry."""

    data = entry.runtime_data
    entities = []

    if data.is_sole:
        # Sole mode: only add Sole-specific control buttons
        for key, name in [
            (SOLE_SPEED_UP, "Speed up"),
            (SOLE_SPEED_DOWN, "Speed down"),
            (SOLE_STOP, "Stop"),
        ]:
            entities.append(
                SoleButtonEntity(
                    entry=entry,
                    description=ButtonEntityDescription(key=key, name=name),
                )
            )
    else:
        # Standard FTMS buttons
        for description in _ENTITIES:
            entities.append(
                FtmsButtonEntity(
                    entry=entry,
                    description=ButtonEntityDescription(key=description),
                )
            )

    async_add_entities(entities)


class FtmsButtonEntity(FtmsEntity, ButtonEntity):
    """Representation of FTMS control buttons."""

    @override
    async def async_press(self) -> None:
        """Handle the button press."""
        if self.key == c.RESET:
            await self.ftms.reset()

        elif self.key == c.START:
            await self.ftms.start_resume()

        elif self.key == c.STOP:
            await self.ftms.stop()

        elif self.key == c.PAUSE:
            await self.ftms.pause()


class SoleButtonEntity(FtmsEntity, ButtonEntity):
    """Sole-specific control buttons (speed up/down, stop)."""

    @override
    async def async_press(self) -> None:
        """Handle the button press."""
        sole = self._data.sole_client
        if sole is None:
            _LOGGER.warning("Sole client not available")
            return

        if self.key == SOLE_SPEED_UP:
            await sole.speed_up()
        elif self.key == SOLE_SPEED_DOWN:
            await sole.speed_down()
        elif self.key == SOLE_STOP:
            await sole.stop_belt()
