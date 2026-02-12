from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol

from .const import DOMAIN

LANGUAGES = {
    "en": "English",
    "nl": "Nederlands",
}

class COPCalculatorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="COP Calculator",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional("language", default="en"): vol.In(LANGUAGES),
                }
            ),
        )

    @callback
    def async_get_options_flow(self, config_entry):
        return COPCalculatorOptionsFlow(config_entry)


class COPCalculatorOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "language",
                        default=self.config_entry.data.get("language", "en"),
                    ): vol.In(LANGUAGES),
                }
            ),
        )
