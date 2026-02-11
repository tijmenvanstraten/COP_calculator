import voluptuous as vol
from homeassistant import config_entries
from .const import DOMAIN

LANG_OPTIONS = ["en", "nl"]

class COPCalculatorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for COP Calculator."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="COP Calculator", data={"language": user_input["language"]})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("language", default="en"): vol.In(LANG_OPTIONS)
            }),
        )