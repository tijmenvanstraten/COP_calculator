"""The COP Calculator integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import async_get_platforms

from .const import DOMAIN

DOMAIN = "cop_calculator"

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the COP Calculator integration."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up COP Calculator from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])

