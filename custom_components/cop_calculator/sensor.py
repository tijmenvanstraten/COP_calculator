from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo
from datetime import datetime
from collections import defaultdict
from .const import DOMAIN

# ============================
# Base sensor class
# ============================
class COPBaseSensor(SensorEntity):
    def __init__(self, language="en"):
        self.lang = language

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "cop_calculator")},
            name="COP Calculator",
            manufacturer="Tijmen van Straten",
            model="Calculated COP",
        )

# ============================
# Power Integrator
# ============================
class PowerIntegrator:
    def __init__(self):
        self.measurements = []

    def add_measurement(self, power, timestamp=None):
        if power is None:
            return
        # Factor check: alles naar W
        if power < 0.01:  # vermoedelijk kW
            power *= 1000
        if timestamp is None:
            timestamp = datetime.now()
        self.measurements.append((timestamp, power))

    def calculate_energy(self):
        if len(self.measurements) < 2:
            return 0
        total_wh = 0
        for i in range(1, len(self.measurements)):
            t1, p1 = self.measurements[i - 1]
            t2, p2 = self.measurements[i]
            dt = (t2 - t1).total_seconds() / 3600
            total_wh += ((p1 + p2) / 2) * dt
        return total_wh / 1000  # kWh

    def calculate_energy_by_month(self):
        by_month = defaultdict(list)
        for t, p in self.measurements:
            by_month[(t.year, t.month)].append((t, p))
        energy = {}
        for key, vals in by_month.items():
            if len(vals) < 2:
                energy[key] = 0
                continue
            total_wh = 0
            for i in range(1, len(vals)):
                t1, p1 = vals[i-1]
                t2, p2 = vals[i]
                dt = (t2 - t1).total_seconds() / 3600
                total_wh += ((p1+p2)/2)*dt
            energy[key] = total_wh / 1000
        return energy

    def calculate_energy_by_year(self):
        by_year = defaultdict(list)
        for t, p in self.measurements:
            by_year[t.year].append((t, p))
        energy = {}
        for key, vals in by_year.items():
            if len(vals) < 2:
                energy[key] = 0
                continue
            total_wh = 0
            for i in range(1, len(vals)):
                t1, p1 = vals[i-1]
                t2, p2 = vals[i]
                dt = (t2 - t1).total_seconds() / 3600
                total_wh += ((p1+p2)/2)*dt
            energy[key] = total_wh / 1000
        return energy

# ============================
# COP Monitor Base
# ============================
class COPMonitor:
    def __init__(self):
        self.electric = PowerIntegrator()
        self.thermal = PowerIntegrator()
        self.active = False

    def start_cycle(self):
        self.electric = PowerIntegrator()
        self.thermal = PowerIntegrator()
        self.active = True

    def update(self, electric_power, thermal_power):
        if self.active:
            self.electric.add_measurement(electric_power)
            self.thermal.add_measurement(thermal_power)

    def end_cycle(self, electric_power, thermal_power):
        self.update(electric_power, thermal_power)
        self.active = False

    def calculate_cop(self):
        e = self.electric.calculate_energy()
        t = self.thermal.calculate_energy()
        return t/e if e > 0 else None

# ============================
# Space Heating Sensor
# ============================
class COPSpaceHeatingSensor(COPBaseSensor):
    def __init__(self, hass, language="en"):
        super().__init__(language)
        self.hass = hass
        self.monitor = COPMonitor()
        self._state = None
        self._attr_unique_id = "cop_space_heating_sensor"

    @property
    def name(self):
        return "COP Space Heating" if self.lang=="en" else "COP Ruimteverwarming"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"

    def update(self):
        try:
            mode = self.hass.states.get("sensor.control_unit_operation_state_2").state
        except AttributeError:
            mode = None

        electric_power = self.get_electric_power()
        thermal_power = self.get_thermal_power()

        if mode == "operation_state_heat_thermo_on":
            if not self.monitor.active:
                self.monitor.start_cycle()
            self.monitor.update(electric_power, thermal_power)
        else:
            if self.monitor.active:
                self.monitor.end_cycle(electric_power, thermal_power)
                self._state = self.monitor.calculate_cop()

    def get_electric_power(self):
        try:
            outside = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            inside = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            total = outside + inside
            if total < 0.01:  # vermoedelijk kW
                total *= 1000
            return total
        except (AttributeError, ValueError, TypeError):
            return 0

    def get_thermal_power(self):
        try:
            outlet = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_2").state)
            inlet = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_2").state)
            flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            delta_t = outlet - inlet
            power = abs(flow * delta_t * 4.18 * 1000 / 3600)
            return power
        except (AttributeError, ValueError, TypeError):
            return None

# ============================
# Cooling Sensor
# ============================
class COPCoolingSensor(COPSpaceHeatingSensor):
    def __init__(self, hass, language="en"):
        super().__init__(hass, language)
        self.monitor = COPMonitor()
        self._attr_unique_id = "cop_cooling_sensor"

    @property
    def name(self):
        return "COP Cooling" if self.lang=="en" else "COP Koeling"

    def update(self):
        try:
            mode = self.hass.states.get("sensor.control_unit_operation_state_2").state
        except AttributeError:
            mode = None

        electric_power = self.get_electric_power()
        thermal_power = self.get_thermal_power()

        if mode == "operation_state_cool_thermo_on":
            if not self.monitor.active:
                self.monitor.start_cycle()
            self.monitor.update(electric_power, thermal_power)
        else:
            if self.monitor.active:
                self.monitor.end_cycle(electric_power, thermal_power)
                self._state = self.monitor.calculate_cop()

    def get_thermal_power(self):
        try:
            outlet = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_koeling").state)
            inlet = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_koeling").state)
            flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            delta_t = outlet - inlet
            power = abs(flow * delta_t * 4.18 * 1000 / 3600)
            return power
        except (AttributeError, ValueError, TypeError):
            return None

# ============================
# DHW Sensor
# ============================
class COPDHWSensor(COPBaseSensor):
    def __init__(self, hass, language="en"):
        super().__init__(language)
        self.hass = hass
        self._state = None
        self._attr_unique_id = "cop_dhw_sensor"
        self.start_temp = None
        self.end_temp = None
        self.total_delta = 0
        self.active = False
        self.start_time = None
        self.end_time = None

    @property
    def name(self):
        return "COP DHW" if self.lang=="en" else "COP DHW"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"

    def update(self):
        try:
            mode = self.hass.states.get("sensor.control_unit_operation_state_2").state
            current_temp = float(self.hass.states.get("sensor.dhw_current_temperature").state)
        except (AttributeError, ValueError, TypeError):
            return

        electric_power = self.get_electric_power()

        if mode == "operation_state_dhw_on":
            if not self.active:
                self.start_cycle(current_temp)
            else:
                self.update_temp(current_temp)
        else:
            if self.active:
                self.end_cycle(current_temp)
                thermal_energy = self.calculate_thermal_energy()
                electric_energy = self.calculate_electric_energy(electric_power)
                self._state = thermal_energy / electric_energy if electric_energy else None

    def start_cycle(self, temp):
        self.start_temp = temp
        self.end_temp = temp
        self.total_delta = 0
        self.active = True
        self.start_time = datetime.now()

    def update_temp(self, temp):
        delta = temp - self.end_temp
        if delta < 0:
            self.total_delta += abs(delta)
        self.end_temp = temp

    def end_cycle(self, temp):
        self.update_temp(temp)
        self.active = False
        self.end_time = datetime.now()

    def calculate_thermal_energy(self):
        if self.start_temp is None or self.end_temp is None:
            return None
        volume = 260
        total_delta = (self.end_temp - self.start_temp) + self.total_delta
        return (volume * total_delta * 4.18) / 3600

    def calculate_electric_energy(self, power):
        if not self.start_time or not self.end_time:
            return None
        duration_h = (self.end_time - self.start_time).total_seconds() / 3600
        return (power * duration_h)/1000

    def get_electric_power(self):
        try:
            outside = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            inside = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            element = 59
            total = outside + (inside - element)
            if total < 0.01:
                total *= 1000
            return total
        except (AttributeError, ValueError, TypeError):
            return 0

# ============================
# COP Periode Sensor (Month/Year/Lifetime)
# ============================
class COPPeriodeSensor(COPBaseSensor):
    def __init__(self, hass, mode, period_type, language="en"):
        super().__init__(language)
        self.hass = hass
        self.mode = mode
        self.period_type = period_type
        self.electric_integrator = PowerIntegrator()
        self.thermal_integrator = PowerIntegrator()
        self._state = None
        self._attributes = {}
        self._attr_unique_id = f"cop_{mode}_{period_type}_sensor"

    @property
    def name(self):
        return f"COP {self.mode} {self.period_type}" if self.lang=="en" else f"COP {self.mode} {self.period_type}"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"

    @property
    def extra_state_attributes(self):
        return self._attributes

    def update(self):
        electric_power = self.get_electric_power()
        thermal_power = self.get_thermal_power()
        self.electric_integrator.add_measurement(electric_power)
        if thermal_power is not None:
            self.thermal_integrator.add_measurement(thermal_power)

        if self.period_type == "maand":
            cop_period = self.calculate_cop_by_month()
        elif self.period_type == "jaar":
            cop_period = self.calculate_cop_by_year()
        elif self.period_type == "lifetime":
            self._state = self.calculate_lifetime_cop()
            return
        else:
            return

        self._attributes = {f"{k[0]}-{k[1]}": v for k,v in cop_period.items()}
        values = [v for v in cop_period.values() if v is not None]
        self._state = sum(values)/len(values) if values else None

    def calculate_cop_by_month(self):
        e = self.electric_integrator.calculate_energy_by_month()
        t = self.thermal_integrator.calculate_energy_by_month()
        cop = {}
        for k in e:
            cop[k] = t.get(k,0)/e[k] if e[k] > 0 else None
        return cop

    def calculate_cop_by_year(self):
        e = self.electric_integrator.calculate_energy_by_year()
        t = self.thermal_integrator.calculate_energy_by_year()
        cop = {}
        for k in e:
            cop[k] = t.get(k,0)/e[k] if e[k] > 0 else None
        return cop

    def calculate_lifetime_cop(self):
        e = self.electric_integrator.calculate_energy()
        t = self.thermal_integrator.calculate_energy()
        return t/e if e>0 else None

    def get_electric_power(self):
        try:
            outside = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            inside = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            total = outside + inside
            if total < 0.01:
                total *= 1000
            return total
        except (AttributeError, ValueError, TypeError):
            return 0

    def get_thermal_power(self):
        try:
            if self.mode=="operation_state_heat_thermo_on":
                outlet = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_2").state)
                inlet = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_2").state)
                flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            elif self.mode=="operation_state_cool_thermo_on":
                outlet = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_koeling").state)
                inlet = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_koeling").state)
                flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            else:
                return None
            return abs(flow * (outlet - inlet) * 4.18 * 1000 / 3600)
        except (AttributeError, ValueError, TypeError):
            return None

# ============================
# Setup
# ============================
async def async_setup_entry(hass, config_entry, async_add_entities):
    lang = config_entry.data.get("language", "en")
    sensors = [
        COPSpaceHeatingSensor(hass, language=lang),
        COPCoolingSensor(hass, language=lang),
        COPDHWSensor(hass, language=lang),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "maand", language=lang),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "jaar", language=lang),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "lifetime", language=lang),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "maand", language=lang),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "jaar", language=lang),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "lifetime", language=lang),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "maand", language=lang),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "jaar", language=lang),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "lifetime", language=lang),
    ]
    async_add_entities(sensors, True)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    sensors = [
        COPSpaceHeatingSensor(hass),
        COPCoolingSensor(hass),
        COPDHWSensor(hass),
    ]
    async_add_entities(sensors, True)