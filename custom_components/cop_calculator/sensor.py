from __future__ import annotations

from datetime import datetime
from typing import Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.dt import now as ha_now

from .const import (
    DOMAIN,
    CONF_POWER_INDOOR,
    CONF_POWER_OUTDOOR,
    CONF_POWER_PUMP,
    CONF_TEMP_INLET,
    CONF_TEMP_OUTLET,
    CONF_OPERATION_STATE,
    CONF_DHW_HEATER_BINARY,
    CONF_BOILER_VOLUME_L,
    CONF_LANGUAGE,
)

WATER_HEAT_CAPACITY_WH_PER_L_K = 1.163  # Wh per liter per Kelvin
SECONDS_PER_HOUR = 3600

class CopEnergyAccumulator(RestoreEntity):
    """Base class for COP energy accumulation."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.NONE
    _attr_native_unit_of_measurement = ""

    def __init__(self, hass: HomeAssistant, name: str, period: str):
        self.hass = hass
        self._attr_name = name
        self.period = period  # "month" | "year" | "lifetime"

        self._thermal_kwh = 0.0
        self._electric_kwh = 0.0
        self._state: Optional[float] = None

        self._last_reset_marker: Optional[str] = None

    async def async_added_to_hass(self):
        last = await self.async_get_last_state()
        if last:
            self._thermal_kwh = float(last.attributes.get("thermal_kwh", 0.0))
            self._electric_kwh = float(last.attributes.get("electric_kwh", 0.0))
            self._last_reset_marker = last.attributes.get("reset_marker")

    def _period_marker(self) -> Optional[str]:
        now = ha_now()
        if self.period == "month":
            return f"{now.year}-{now.month}"
        if self.period == "year":
            return str(now.year)
        return None  # lifetime

    def _check_reset(self):
        marker = self._period_marker()
        if marker and marker != self._last_reset_marker:
            self._thermal_kwh = 0.0
            self._electric_kwh = 0.0
            self._last_reset_marker = marker

    def add_energy(self, thermal_kwh: float, electric_kwh: float):
        self._check_reset()
        self._thermal_kwh += max(thermal_kwh, 0.0)
        self._electric_kwh += max(electric_kwh, 0.0)

        if self._electric_kwh > 0:
            self._state = round(self._thermal_kwh / self._electric_kwh, 3)
        else:
            self._state = None

        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return {
            "thermal_kwh": round(self._thermal_kwh, 3),
            "electric_kwh": round(self._electric_kwh, 3),
            "reset_marker": self._last_reset_marker,
        }

class RealtimeCopSensor(SensorEntity):
    """Realtime COP based on power (W)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.NONE
    _attr_native_unit_of_measurement = ""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        thermal_power_fn,
        electric_power_fn,
    ):
        self.hass = hass
        self._attr_name = name
        self._thermal_power_fn = thermal_power_fn
        self._electric_power_fn = electric_power_fn
        self._state: Optional[float] = None

    async def async_update(self):
        thermal_w = self._thermal_power_fn()
        electric_w = self._electric_power_fn()

        if thermal_w > 0 and electric_w > 0:
            self._state = round(thermal_w / electric_w, 2)
        else:
            self._state = 0.0

    @property
    def native_value(self):
        return self._state

def calculate_dhw_thermal_power_w(
    inlet_temp: float,
    outlet_temp: float,
    boiler_volume_l: float,
    last_outlet_temp: Optional[float],
    delta_seconds: float,
) -> float:
    """
    DHW thermal power based on tank temperature increase.
    Uses outlet temperature only.
    """

    if last_outlet_temp is None:
        return 0.0

    delta_t = outlet_temp - last_outlet_temp
    if delta_t <= 0:
        return 0.0

    energy_wh = (
        boiler_volume_l
        * WATER_HEAT_CAPACITY_WH_PER_L_K
        * delta_t
    )

    return (energy_wh / delta_seconds) * SECONDS_PER_HOUR

