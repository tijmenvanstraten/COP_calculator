import logging
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.const import CONF_NAME
from homeassistant.util import dt as dt_util
from datetime import timedelta
import json
from .const import (
    DOMAIN, SENSOR_INDOOR_POWER, SENSOR_OUTDOOR_POWER, SENSOR_DHW_HEATER,
    SENSOR_OUTLET_TEMP, SENSOR_INLET_TEMP, SENSOR_FLOW, SENSOR_OPERATION_STATE,
    SENSOR_DHW_CURRENT_TEMP, SENSOR_DHW_TARGET_TEMP,
    STATE_HEAT_THERMO, STATE_HEAT_COOL, STATE_DHW,
    ATTR_PUMP_POWER, ATTR_DHW_TANK_VOLUME,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the COP sensors."""
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    pump_power = hass.data[DOMAIN][entry.entry_id]["pump_power"]
    dhw_tank_volume = hass.data[DOMAIN][entry.entry_id]["dhw_tank_volume"]

    coordinator = HitachiYutakiCOPDataUpdateCoordinator(
        hass, store, pump_power, dhw_tank_volume
    )
    await coordinator.async_config_entry_first_refresh()

    # Realtime COP sensors
    sensors = [
        HitachiYutakiCOPSensor(coordinator, "Heating", "realtime"),
        HitachiYutakiCOPSensor(coordinator, "Cooling", "realtime"),
    ]
    # DHW per run sensor
    sensors.append(HitachiYutakiDHWRunSensor(coordinator))
    # Maand/jaar/lifetime COP sensors
    for mode in ["Heating", "Cooling", "DHW"]:
        for period in ["Monthly", "Yearly", "Lifetime"]:
            sensors.append(HitachiYutakiCOPSensor(coordinator, mode, period.lower()))

    async_add_entities(sensors, True)

class HitachiYutakiCOPDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass, store, pump_power, dhw_tank_volume):
        """Initialize."""
        self._hass = hass
        self._store = store
        self._pump_power = pump_power
        self._dhw_tank_volume = dhw_tank_volume
        self._data = {
            "heating": {"electrical": 0, "thermal": 0},
            "cooling": {"electrical": 0, "thermal": 0},
            "dhw": {"electrical": 0, "thermal": 0, "runs": []},
            "monthly": {"heating": {"electrical": 0, "thermal": 0}, "cooling": {"electrical": 0, "thermal": 0}, "dhw": {"electrical": 0, "thermal": 0}},
            "yearly": {"heating": {"electrical": 0, "thermal": 0}, "cooling": {"electrical": 0, "thermal": 0}, "dhw": {"electrical": 0, "thermal": 0}},
            "lifetime": {"heating": {"electrical": 0, "thermal": 0}, "cooling": {"electrical": 0, "thermal": 0}, "dhw": {"electrical": 0, "thermal": 0}},
        }
        self._current_dhw_run = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self):
        """Update data."""
        try:
            # Load persistent data
            if (data := await self._store.async_load()) is not None:
                self._data.update(data)

            # Get current sensor values
            indoor_power = float(self._hass.states.get(SENSOR_INDOOR_POWER).state)
            outdoor_power = float(self._hass.states.get(SENSOR_OUTDOOR_POWER).state)
            dhw_heater = self._hass.states.get(SENSOR_DHW_HEATER).state == "on"
            outlet_temp = float(self._hass.states.get(SENSOR_OUTLET_TEMP).state)
            inlet_temp = float(self._hass.states.get(SENSOR_INLET_TEMP).state)
            flow = float(self._hass.states.get(SENSOR_FLOW).state)
            operation_state = self._hass.states.get(SENSOR_OPERATION_STATE).state
            dhw_current_temp = float(self._hass.states.get(SENSOR_DHW_CURRENT_TEMP).state)
            dhw_target_temp = float(self._hass.states.get(SENSOR_DHW_TARGET_TEMP).state)

            # Determine current mode
            mode = None
            if STATE_HEAT_THERMO in operation_state:
                mode = "heating"
            elif STATE_HEAT_COOL in operation_state:
                mode = "cooling"
            elif STATE_DHW in operation_state:
                mode = "dhw"

            # DHW run logic
            if STATE_DHW in operation_state or dhw_heater:
                if self._current_dhw_run is None:
                    self._current_dhw_run = {
                        "start_time": dt_util.now(),
                        "start_temp": dhw_current_temp,
                        "electrical": 0,
                        "thermal": 0,
                    }
                # Assign electrical power
                if mode == "heating":
                    self._data[mode]["electrical"] += (outdoor_power + self._pump_power) / (60 * 60 * 2)
                elif mode == "cooling":
                    self._data[mode]["electrical"] += (outdoor_power + self._pump_power) / (60 * 60 * 2)
                elif mode == "dhw":
                    self._data[mode]["electrical"] += outdoor_power / (60 * 60 * 2)

                # Elektrisch element always to DHW if heater is on
                if dhw_heater:
                    self._data["dhw"]["electrical"] += indoor_power / (60 * 60 * 2)

                # Thermal power for DHW: account for temp changes
                if self._current_dhw_run is not None:
                    delta_temp = dhw_current_temp - self._current_dhw_run["start_temp"]
                    if delta_temp > 0:
                        self._current_dhw_run["thermal"] += (delta_temp * self._dhw_tank_volume * 4.18) / (60 * 60 * 1000)  # kWh
                    # End of run: neither DHW mode nor heater is active
                    if STATE_DHW not in operation_state and not dhw_heater:
                        self._data["dhw"]["runs"].append(self._current_dhw_run)
                        self._current_dhw_run = None
            else:
                self._current_dhw_run = None

            # Thermal power for heating/cooling
            if mode == "heating" or mode == "cooling":
                delta_t = outlet_temp - inlet_temp
                thermal_power = (delta_t * flow * 4.18) / (60 * 60 * 1000)  # kWh per 30s
                self._data[mode]["thermal"] += thermal_power

            # Update monthly/yearly/lifetime
            now = dt_util.now()
            for period in ["monthly", "yearly", "lifetime"]:
                if mode in ["heating", "cooling", "dhw"]:
                    self._data[period][mode]["electrical"] += self._data[mode]["electrical"]
                    self._data[period][mode]["thermal"] += self._data[mode]["thermal"]

            # Save persistent data
            await self._store.async_save(self._data)

            return self._data
        except Exception as err:
            raise UpdateFailed(f"Error updating COP data: {err}")

class HitachiYutakiCOPSensor(Entity):
    """Representation of a Hitachi Yutaki COP sensor."""

    def __init__(self, coordinator, mode, period):
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._mode = mode
        self._period = period
        self._attr_name = f"Hitachi Yutaki {mode} {period} COP"
        self._attr_unique_id = f"hitachi_yutaki_{mode.lower()}_{period}_cop"

    @property
    def state(self):
        """Return the state of the sensor."""
        data = self._coordinator.data
        if self._period == "realtime":
            electrical = data[self._mode.lower()]["electrical"]
            thermal = data[self._mode.lower()]["thermal"]
        else:
            electrical = data[self._period][self._mode.lower()]["electrical"]
            thermal = data[self._period][self._mode.lower()]["thermal"]
        return round(thermal / electrical, 2) if electrical > 0 else None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        data = self._coordinator.data
        if self._period == "realtime":
            return {
                "electrical_energy": data[self._mode.lower()]["electrical"],
                "thermal_energy": data[self._mode.lower()]["thermal"],
            }
        else:
            return {
                "electrical_energy": data[self._period][self._mode.lower()]["electrical"],
                "thermal_energy": data[self._period][self._mode.lower()]["thermal"],
            }

class HitachiYutakiDHWRunSensor(Entity):
    """Representation of a Hitachi Yutaki DHW run COP sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._attr_name = "Hitachi Yutaki DHW Run COP"
        self._attr_unique_id = "hitachi_yutaki_dhw_run_cop"

    @property
    def state(self):
        """Return the state of the sensor."""
        if not self._coordinator.data["dhw"]["runs"]:
            return None
        last_run = self._coordinator.data["dhw"]["runs"][-1]
        return round(last_run["thermal"] / last_run["electrical"], 2) if last_run["electrical"] > 0 else None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if not self._coordinator.data["dhw"]["runs"]:
            return {}
        last_run = self._coordinator.data["dhw"]["runs"][-1]
        return {
            "start_time": last_run["start_time"],
            "start_temp": last_run["start_temp"],
            "electrical_energy": last_run["electrical"],
            "thermal_energy": last_run["thermal"],
        }
