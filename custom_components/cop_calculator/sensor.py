import logging
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity
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

# --- Helper functions ---
def _get_state_str(hass, entity_id):
    state = hass.states.get(entity_id)
    return state.state if state else None

def _get_state_float(hass, entity_id):
    state = hass.states.get(entity_id)
    if not state:
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None

# --- Coordinator ---
class HitachiYutakiCOPDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, store, pump_power, dhw_tank_volume):
        self._hass = hass
        self._store = store
        self._pump_power = pump_power
        self._dhw_tank_volume = dhw_tank_volume
        self._current_dhw_run = None
        
        # --- Initial data structure ---
        self._data = {}
        for key in ["heating", "cooling", "dhw"]:
            self._data.setdefault(key, {})

            self._data[key].setdefault("power", {})
            self._data[key]["power"].setdefault("electrical", 0)
            self._data[key]["power"].setdefault("thermal", 0)

            self._data[key].setdefault("energy", {})
            self._data[key]["energy"].setdefault("electrical", 0)
            self._data[key]["energy"].setdefault("thermal", 0)

            if key == "dhw":
                self._data[key].setdefault("runs", [])
            else:
                self._data[key].setdefault("runs", None)

            self._data[key].setdefault("last_energy_thermal", 0)
            self._data[key].setdefault("last_energy_electrical", 0)


        for period in ["monthly", "yearly", "lifetime"]:
            self._data[period] = {}
            for key in ["heating", "cooling", "dhw"]:
                self._data[period][key] = {"energy": {"electrical": 0, "thermal": 0}}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self):
        """Update COP data safely."""
        try:
            # Persistent data
            if (data := await self._store.async_load()) is not None:
                self._data.update(data)

            # --- Ensure baseline keys exist ---
            for key in ["heating","cooling","dhw"]:
                self._data[key] = {
                    "power": self._data.get(key, {}).get("power", {"electrical":0,"thermal":0}),
                    "energy": self._data.get(key, {}).get("energy", {"electrical":0,"thermal":0}),
                    "runs": self._data.get(key, {}).get("runs", [] if key=="dhw" else None),
                    "last_energy_thermal": self._data.get(key, {}).get("last_energy_thermal", 0),
                    "last_energy_electrical": self._data.get(key, {}).get("last_energy_electrical", 0)
                }

            for period in ["monthly", "yearly", "lifetime"]:
                self._data.setdefault(period, {})
                for key in ["heating", "cooling", "dhw"]:
                    self._data[period].setdefault(key, {})
                    self._data[period][key].setdefault("energy", {})
                    self._data[period][key]["energy"].setdefault("electrical", 0)
                    self._data[period][key]["energy"].setdefault("thermal", 0)

            # --- Central input reading ---
            indoor_power = _get_state_float(self._hass, SENSOR_INDOOR_POWER)
            outdoor_power = _get_state_float(self._hass, SENSOR_OUTDOOR_POWER)
            dhw_heater = _get_state_str(self._hass, SENSOR_DHW_HEATER) == "on"
            outlet_temp = _get_state_float(self._hass, SENSOR_OUTLET_TEMP)
            inlet_temp = _get_state_float(self._hass, SENSOR_INLET_TEMP)
            flow = _get_state_float(self._hass, SENSOR_FLOW)
            operation_state = _get_state_str(self._hass, SENSOR_OPERATION_STATE)
            dhw_current_temp = _get_state_float(self._hass, SENSOR_DHW_CURRENT_TEMP)
            dhw_target_temp = _get_state_float(self._hass, SENSOR_DHW_TARGET_TEMP)

            # --- Check input sensors ---
            critical = [
                indoor_power, outdoor_power, outlet_temp, inlet_temp,
                flow, dhw_current_temp, dhw_target_temp, operation_state
            ]
            if any(v is None for v in critical):
                _LOGGER.debug("COP calculator: input sensor(s) niet beschikbaar, update skipped")
                return self._data

            # --- Debug logging ---
            _LOGGER.debug(f"Sensor values - indoor_power: {indoor_power}, outdoor_power: {outdoor_power}, "
                          f"outlet_temp: {outlet_temp}, inlet_temp: {inlet_temp}, flow: {flow}, "
                          f"operation_state: {operation_state}, dhw_heater: {dhw_heater}")
                          
            # --- Determine current mode ---
            mode = None
            if STATE_HEAT_THERMO in operation_state:
                mode = "heating"
            elif STATE_HEAT_COOL in operation_state:
                mode = "cooling"
            elif STATE_DHW in operation_state:
                mode = "dhw"

            if mode is None:
                return self._data

            # --- Realtime COP for heating/cooling ---
            if mode in ["heating", "cooling"]:
                delta_t = outlet_temp - inlet_temp
                flow_kg_s = flow * 1000 / 3600  # m3/h -> kg/s
                thermal_power_w = abs(flow_kg_s * 4180 * delta_t)  # W = m_dot * Cp * dT
                electrical_power_w = outdoor_power + self._pump_power

                self._data[mode]["power"]["thermal"] = thermal_power_w
                self._data[mode]["power"]["electrical"] = electrical_power_w

                # cumulative energy (kWh)
                interval_h = self.update_interval.total_seconds() / 3600
                self._data[mode]["energy"]["thermal"] += thermal_power_w * interval_h / 1000
                self._data[mode]["energy"]["electrical"] += electrical_power_w * interval_h / 1000
            
            # --- DHW-run logic ---
            elif mode == "dhw" or dhw_heater:
                if self._current_dhw_run is None and dhw_current_temp is not None:
                    # Start nieuwe DHW-run
                    self._current_dhw_run = {
                        "start_time": dt_util.now(),
                        "start_temp": dhw_current_temp,
                        "last_temp": dhw_current_temp,
                        "electrical": 0,
                        "thermal": 0,
                        "sum_of_drops": 0,  # cumulatieve dalingen
                    }

            if self._current_dhw_run is not None and dhw_current_temp is not None:
                # Bereken verandering t.o.v. vorige temp
                delta_temp = dhw_current_temp - self._current_dhw_run["last_temp"]
                
                # registreer dalingen
                if delta_temp < 0:
                    self._current_dhw_run["sum_of_drops"] += abs(delta_temp)

                # update laatste gemeten temp
                self._current_dhw_run["last_temp"] = dhw_current_temp

                # Elektrical energy tijdens run
                dhw_electric = 0
                interval_hours = self.update_interval.total_seconds() / 3600
                if dhw_heater and indoor_power is not None:
                    dhw_electric += indoor_power * interval_hours  # kWh
                if mode == "dhw" and outdoor_power is not None:
                    dhw_electric += outdoor_power * interval_hours  # kWh
                self._current_dhw_run["electrical"] += dhw_electric
                
            # --- End DHW-run ---
            if self._current_dhw_run is not None and mode != "dhw" and not dhw_heater:
                start_temp = self._current_dhw_run["start_temp"]
                end_temp = self._current_dhw_run["last_temp"]
                thermal_delta_temp = (end_temp - start_temp) + self._current_dhw_run["sum_of_drops"]
            
                thermal_increment = thermal_delta_temp * self._dhw_tank_volume * 4180 / 3600000  # kWh
                electrical_increment = self._current_dhw_run["electrical"]
                
                # --- update total DHW energy ---
                self._data["dhw"]["energy"]["thermal"] += thermal_increment
                self._data["dhw"]["energy"]["electrical"] += electrical_increment

                # --- update period energies (exact once per run) ---
                for period in ["monthly", "yearly", "lifetime"]:
                    self._data[period]["dhw"]["energy"]["thermal"] += thermal_increment
                    self._data[period]["dhw"]["energy"]["electrical"] += electrical_increment

                # --- store run ---
                self._current_dhw_run["thermal"] = thermal_increment
                self._data["dhw"]["runs"].append(self._current_dhw_run)

                # --- sync last_energy to avoid future deltas ---
                self._data["dhw"]["last_energy_thermal"] = self._data["dhw"]["energy"]["thermal"]
                self._data["dhw"]["last_energy_electrical"] = self._data["dhw"]["energy"]["electrical"]

                self._current_dhw_run = None

            # --- Update period COP values using delta ---
            for key in ["heating", "cooling", "dhw"]:
                self._data[key].setdefault("last_energy_thermal", 0)
                self._data[key].setdefault("last_energy_electrical", 0)

            for period in ["monthly","yearly","lifetime"]:
                if mode in ["heating", "cooling"]:
                    thermal_delta = self._data[mode]["energy"]["thermal"] - self._data[mode]["last_energy_thermal"]
                    electrical_delta = self._data[mode]["energy"]["electrical"] - self._data[mode]["last_energy_electrical"]

                    self._data[period][mode]["energy"]["thermal"] += thermal_delta
                    self._data[period][mode]["energy"]["electrical"] += electrical_delta
    
            # --- Save last cumulative values ---
            self._data[mode]["last_energy_thermal"] = self._data[mode]["energy"]["thermal"]
            self._data[mode]["last_energy_electrical"] = self._data[mode]["energy"]["electrical"]
        
            # Save persistent data
            await self._store.async_save(self._data)
            return self._data

        except Exception as err:
            raise UpdateFailed(f"Error updating COP data: {err}")


# --- Setup entry ---
async def async_setup_entry(hass, entry, async_add_entities):
    store = Store(hass, version=1, key=DOMAIN)
    pump_power = hass.data[DOMAIN][entry.entry_id]["pump_power"]
    dhw_tank_volume = hass.data[DOMAIN][entry.entry_id]["dhw_tank_volume"]

    coordinator = HitachiYutakiCOPDataUpdateCoordinator(
        hass, store, pump_power, dhw_tank_volume
    )

    # Entities
    sensors = [
        HitachiYutakiCOPSensor(coordinator, "Heating", "realtime"),
        HitachiYutakiCOPSensor(coordinator, "Cooling", "realtime"),
        HitachiYutakiDHWRunSensor(coordinator),
    ]
    for mode in ["Heating", "Cooling", "DHW"]:
        for period in ["Monthly", "Yearly", "Lifetime"]:
            sensors.append(HitachiYutakiCOPSensor(coordinator, mode, period.lower()))

    async_add_entities(sensors, True)

    # First refresh as background rask 
    hass.async_create_task(coordinator.async_config_entry_first_refresh())

class HitachiYutakiCOPSensor(CoordinatorEntity, Entity):
    """Representation of a Hitachi Yutaki COP sensor."""

    def __init__(self, coordinator, mode, period):
        """Initialize the sensor."""
        super().__init__(coordinator) 
        self._coordinator = coordinator
        self._mode = mode.lower()
        self._period = period.lower()
        self._attr_name = f"Hitachi Yutaki {mode} {period} COP"
        self._attr_unique_id = f"hitachi_yutaki_{mode.lower()}_{period}_cop"
        self._attr_state_class = "measurement"
        
    @property
    def state(self):
        """Return the state of the sensor."""
        data = self._coordinator.data
        if not data:
            return None
        if self._period == "realtime":
            electrical = data.get(self._mode, {}).get("power", {}).get("electrical", 0)
            thermal = data.get(self._mode, {}).get("power", {}).get("thermal", 0)
        else:
            electrical = data.get(self._period, {}).get(self._mode, {}).get("energy", {}).get("electrical", 0)
            thermal = data.get(self._period, {}).get(self._mode, {}).get("energy", {}).get("thermal", 0)
        return round(thermal / electrical, 2) if electrical > 0 else None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        data = self._coordinator.data
        if not data:
            return None
        if self._period == "realtime":
            return {
                "electrical_energy": data.get(self._mode, {}).get("power", {}).get("electrical", 0),
                "thermal_energy": data.get(self._mode, {}).get("power", {}).get("thermal", 0),
            }
        else:
            return {
                "electrical_energy": data.get(self._period, {}).get(self._mode, {}).get("energy", {}).get("electrical", 0),
                "thermal_energy": data.get(self._period, {}).get(self._mode, {}).get("energy", {}).get("thermal", 0),
            }

class HitachiYutakiDHWRunSensor(CoordinatorEntity, Entity):
    """Representation of a Hitachi Yutaki DHW run COP sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_name = "Hitachi Yutaki DHW Run COP"
        self._attr_unique_id = "hitachi_yutaki_dhw_run_cop"
        self._attr_state_class = "measurement"

    @property
    def state(self):
        """Return the state of the sensor."""
        data = self._coordinator.data
        if not data or not data.get("dhw", {}).get("runs"):
            return None  # of {}
        last_run = self._coordinator.data["dhw"]["runs"][-1]
        return round(last_run["thermal"] / last_run["electrical"], 2) if last_run["electrical"] > 0 else None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        data = self._coordinator.data
        if not data or not data.get("dhw", {}).get("runs"):
            return {}
        last_run = self._coordinator.data["dhw"]["runs"][-1]
        return {
            "start_time": last_run["start_time"],
            "start_temp": last_run["start_temp"],
            "electrical_energy": last_run["electrical"],
            "thermal_energy": last_run["thermal"],
        }
