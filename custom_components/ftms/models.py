"""FTMS integration models."""

from __future__ import annotations

import dataclasses as dc
from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from pyftms import FitnessMachine

from .coordinator import DataCoordinator

if TYPE_CHECKING:
    from .sole_client import SoleClient


@dc.dataclass(frozen=True, kw_only=True)
class FtmsData:
    """Data for the FTMS integration."""

    entry_id: str
    unique_id: str
    device_info: DeviceInfo
    ftms: FitnessMachine | None
    coordinator: DataCoordinator
    sensors: list[str]
    is_sole: bool = False
    sole_client: SoleClient | None = None
    external_hr_entity: str | None = None
