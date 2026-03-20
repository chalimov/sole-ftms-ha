"""Diagnostics support for the FTMS integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import FtmsConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FtmsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    data = entry.runtime_data
    ftms = data.ftms

    diag: dict[str, Any] = {
        "connection": {
            "address": entry.data.get("address"),
            "connected": ftms is not None and ftms.is_connected,
        },
        "configuration": {
            "sensors": data.sensors,
            "external_hr_entity": data.external_hr_entity,
        },
    }

    if ftms is not None and ftms.is_connected:
        try:
            dev_info = ftms.device_info
        except AttributeError:
            dev_info = {}

        diag["device"] = {
            "manufacturer": dev_info.get("manufacturer"),
            "model": dev_info.get("model"),
            "sw_version": dev_info.get("sw_version"),
            "hw_version": dev_info.get("hw_version"),
            "machine_type": ftms.machine_type.name if ftms.machine_type else None,
            "available_properties": list(ftms.available_properties),
        }

        try:
            diag["device"]["supported_settings"] = str(ftms.supported_settings)
            diag["device"]["supported_ranges"] = {
                k: {"min": r.min_value, "max": r.max_value, "step": r.step}
                for k, r in ftms.supported_ranges.items()
            }
        except AttributeError:
            pass

    diag["sole"] = {
        "is_sole": data.is_sole,
        "hybrid_active": data.sole_client is not None,
    }

    if data.sole_client is not None:
        diag["sole"]["subscribed"] = data.sole_client._subscribed
        diag["sole"]["pending_writes"] = len(data.sole_client._pending_writes)

    return diag
