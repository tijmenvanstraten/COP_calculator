import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import entity_platform
from homeassistant.helpers.storage import Store
from .const import DOMAIN, DEFAULT_PUMP_POWER, DEFAULT_DHW_TANK_VOLUME

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Hitachi Yutaki COP integration."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up the integration from a config entry."""
    hass.data[DOMAIN][entry.entry_id] = {
        "pump_power": entry.data.get(ATTR_PUMP_POWER, DEFAULT_PUMP_POWER),
        "dhw_tank_volume": entry.data.get(ATTR_DHW_TANK_VOLUME, DEFAULT_DHW_TANK_VOLUME),
        "store": Store(hass, version=1, key=DOMAIN),
    }
    await entity_platform.async_setup_platforms(hass, entry, ["sensor"])
    return True
