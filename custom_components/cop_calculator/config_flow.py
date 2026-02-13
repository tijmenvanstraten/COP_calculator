from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN

class COPCalculatorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="COP Calculator",
                data=user_input,
            )

        schema = vol.Schema({
            vol.Optional(
                "language",
                default="en"
            ): vol.In(["en", "nl"]),

            vol.Optional(
                "circulation_pump_power",
                default=59
            ): vol.Coerce(int),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
        )
