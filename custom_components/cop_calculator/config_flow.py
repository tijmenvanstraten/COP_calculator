from homeassistant import config_entries
import voluptuous as vol

from .const import (
    DOMAIN,
    CONF_LANGUAGE,
    CONF_DHW_VOLUME,
    CONF_HEATER_OFFSET,
    DEFAULT_LANGUAGE,
    DEFAULT_DHW_VOLUME,
    DEFAULT_HEATER_OFFSET,
)


class COPCalculatorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="COP Calculator",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                    ["en", "nl"]
                ),
                vol.Optional(
                    CONF_DHW_VOLUME, default=DEFAULT_DHW_VOLUME
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_HEATER_OFFSET, default=DEFAULT_HEATER_OFFSET
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)
