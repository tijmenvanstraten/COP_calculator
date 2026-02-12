from .const import DOMAIN

async def async_setup(hass, config):
    """Set up via YAML (deprecated)."""
    return True

async def async_setup_entry(hass, entry):
    """Set up COP Calculator from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
