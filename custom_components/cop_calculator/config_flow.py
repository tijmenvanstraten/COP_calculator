from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
from .const import DOMAIN, ATTR_PUMP_POWER, ATTR_DHW_TANK_VOLUME, DEFAULT_PUMP_POWER, DEFAULT_DHW_TANK_VOLUME

class HitachiYutakiCOPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hitachi Yutaki COP."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(
                title="Hitachi Yutaki COP",
                data={
                    ATTR_PUMP_POWER: user_input.get(ATTR_PUMP_POWER, DEFAULT_PUMP_POWER),
                    ATTR_DHW_TANK_VOLUME: user_input.get(ATTR_DHW_TANK_VOLUME, DEFAULT_DHW_TANK_VOLUME),
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(ATTR_PUMP_POWER, default=DEFAULT_PUMP_POWER): int,
                vol.Required(ATTR_DHW_TANK_VOLUME, default=DEFAULT_DHW_TANK_VOLUME): int,
            }),
        )
