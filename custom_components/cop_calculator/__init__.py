"""The COP Calculator integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

DOMAIN = "cop_calculator"

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the COP Calculator integration."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up COP Calculator from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    return True
