import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import entity_platform
from homeassistant.helpers.storage import Store
from homeassistant.config_entries import ConfigEntry  # <-- Voeg dit toe
from homeassistant.exceptions import ConfigEntryNotReady
from .const import DOMAIN, ATTR_PUMP_POWER, ATTR_DHW_TANK_VOLUME, DEFAULT_PUMP_POWER, DEFAULT_DHW_TANK_VOLUME  # <-- Voeg ATTR_PUMP_POWER en ATTR_DHW_TANK_VOLUME toe

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Hitachi Yutaki COP integration."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up the integration from a config entry."""
        # Controleer of de vereiste integratie(s) geladen zijn
    if "hitachi_yutaki" not in hass.config_entries.async_domains():
        raise ConfigEntryNotReady("Dependency 'hitachi_yutaki' not loaded yet")

    hass.data[DOMAIN][entry.entry_id] = {
        "pump_power": entry.data.get(ATTR_PUMP_POWER, DEFAULT_PUMP_POWER),
        "dhw_tank_volume": entry.data.get(ATTR_DHW_TANK_VOLUME, DEFAULT_DHW_TANK_VOLUME),
    }

    # Gebruik async_forward_entry_setups in plaats van async_setup_platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True
