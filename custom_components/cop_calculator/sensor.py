from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity import DeviceInfo
from datetime import datetime
from collections import defaultdict
from .const import DOMAIN


# =========================================================
# Utility: Power → Energy integrator (internal only)
# =========================================================
class PowerIntegrator:
    def __init__(self):
        self.measurements: list[tuple[datetime, float]] = []

    def add(self, power_w: float | None, timestamp: datetime | None = None):
        if power_w is None:
            return

        # Safety: normalize to W if value looks like kW
        if power_w < 0.01:
            power_w *= 1000

        if timestamp is None:
            timestamp = datetime.now()

        self.measurements.append((timestamp, power_w))

    def _integrate(self, values: list[tuple[datetime, float]]) -> float:
        if len(values) < 2:
            return 0.0

        total_wh = 0.0
        for i in range(1, len(values)):
            t1, p1 = values[i - 1]
            t2, p2 = values[i]
            dt_h = (t2 - t1).total_seconds() / 3600
            total_wh += ((p1 + p2) / 2) * dt_h

        return total_wh / 1000  # kWh

    def energy_total(self) -> float:
        return self._integrate(self.measurements)

    def energy_by_month(self) -> dict[tuple[int, int], float]:
        buckets = defaultdict(list)
        for t, p in self.measurements:
            buckets[(t.year, t.month)].append((t, p))

        return {k: self._integrate(v) for k, v in buckets.items()}

    def energy_by_year(self) -> dict[int, float]:
        buckets = defaultdict(list)
        for t, p in self.measurements:
            buckets[t.year].append((t, p))

        return {k: self._integrate(v) for k, v in buckets.items()}


# =========================================================
# Base sensor
# =========================================================
class COPBaseSensor(SensorEntity, RestoreEntity):
    _attr_icon = "mdi:calculator"
    _attr_native_unit_of_measurement = "COP"

    def __init__(self, hass, language: str = "en"):
        self.hass = hass
        self.lang = language
        self._state = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "cop_calculator")},
            name="COP Calculator",
            manufacturer="Tijmen van Straten",
            model="Calculated COP",
        )

    @property
    def native_value(self):
        return self._state


# =========================================================
# Realtime COP (power based)
# =========================================================
class RealtimeCOPMonitor:
    def __init__(self):
        self.electric = PowerIntegrator()
        self.thermal = PowerIntegrator()
        self.active = False

    def start(self):
        self.electric = PowerIntegrator()
        self.thermal = PowerIntegrator()
        self.active = True

    def update(self, electric_w: float, thermal_w: float):
        if not self.active:
            return
        self.electric.add(electric_w)
        self.thermal.add(thermal_w)

    def stop(self, electric_w: float, thermal_w: float):
        self.update(electric_w, thermal_w)
        self.active = False

    def cop(self) -> float | None:
        e = self.electric.energy_total()
        t = self.thermal.energy_total()
        return t / e if e > 0 else None


# =========================================================
# Space Heating & Cooling Realtime COP
# =========================================================
class COPRealtimeSensor(COPBaseSensor):
    def __init__(self, hass, mode: str, language="en"):
        super().__init__(hass, language)
        self.mode = mode
        self.monitor = RealtimeCOPMonitor()
        self._attr_unique_id = f"cop_realtime_{mode}"

    @property
    def name(self):
        if self.mode == "heat":
            return "COP Realtime Heating" if self.lang == "en" else "COP Realtime Verwarmen"
        return "COP Realtime Cooling" if self.lang == "en" else "COP Realtime Koelen"

    async def async_update(self):
        state = self.hass.states.get("sensor.control_unit_operation_state_2")
        if not state:
            return

        electric = self._electric_power_total()
        thermal = self._thermal_power()

        if (
            (self.mode == "heat" and state.state == "operation_state_heat_thermo_on")
            or (self.mode == "cool" and state.state == "operation_state_cool_thermo_on")
        ):
            if not self.monitor.active:
                self.monitor.start()
            self.monitor.update(electric, thermal)
        else:
            if self.monitor.active:
                self.monitor.stop(electric, thermal)
                self._state = self.monitor.cop()

    def _electric_power_total(self) -> float:
        try:
            out = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_buitenunit_active_power").state)
            inn = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_binnenunit_active_power").state)
            return out + inn
        except Exception:
            return 0.0

    def _thermal_power(self) -> float:
        try:
            outlet = float(self.hass.states.get(
                "sensor.control_unit_water_outlet_temperature_2").state)
            inlet = float(self.hass.states.get(
                "sensor.control_unit_water_inlet_temperature_2").state)
            flow = float(self.hass.states.get(
                "sensor.control_unit_water_flow_2").state)
            return abs(flow * (outlet - inlet) * 4.18 * 1000 / 3600)
        except Exception:
            return 0.0


# =========================================================
# DHW COP (cycle based)
# =========================================================
class COPDHWSensor(COPBaseSensor):
    def __init__(self, hass, pump_power_w=59, language="en"):
        super().__init__(hass, language)
        self._attr_unique_id = "cop_dhw"
        self.pump_power = pump_power_w

        self.start_temp = None
        self.last_temp = None
        self.temp_losses = 0.0
        self.start_time = None
        self.end_time = None
        self.active = False

    @property
    def name(self):
        return "COP DHW"

    async def async_update(self):
        try:
            temp = float(self.hass.states.get(
                "sensor.dhw_current_temperature").state)
        except Exception:
            return

        heater_on = self.hass.states.get(
            "binary_sensor.control_unit_dhw_heater_2")
        heater_active = heater_on and heater_on.state == "on"

        if heater_active or self._temp_rising(temp):
            if not self.active:
                self._start_cycle(temp)
            else:
                self._update_temp(temp)
        else:
            if self.active:
                self._end_cycle(temp)
                self._state = self._calculate_cop()

    def _start_cycle(self, temp):
        self.start_temp = temp
        self.last_temp = temp
        self.temp_losses = 0.0
        self.start_time = datetime.now()
        self.active = True

    def _update_temp(self, temp):
        delta = temp - self.last_temp
        if delta < 0:
            self.temp_losses += abs(delta)
        self.last_temp = temp

    def _end_cycle(self, temp):
        self._update_temp(temp)
        self.end_time = datetime.now()
        self.active = False

    def _temp_rising(self, temp) -> bool:
        return self.last_temp is not None and temp > self.last_temp

    def _calculate_cop(self) -> float | None:
        thermal = self._thermal_energy()
        electric = self._electric_energy()
        return thermal / electric if electric and electric > 0 else None

    def _thermal_energy(self) -> float:
        volume_l = 260
        total_delta = (self.last_temp - self.start_temp) + self.temp_losses
        return (volume_l * total_delta * 4.18) / 3600

    def _electric_energy(self) -> float | None:
        if not self.start_time or not self.end_time:
            return None

        duration_h = (self.end_time - self.start_time).total_seconds() / 3600

        try:
            inside = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_binnenunit_active_power").state)
            outside = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_buitenunit_active_power").state)
        except Exception:
            return None

        heater_on = self.hass.states.get(
            "binary_sensor.control_unit_dhw_heater_2")
        if heater_on and heater_on.state == "on":
            power = inside - self.pump_power
        else:
            power = outside

        return (power * duration_h) / 1000


# =========================================================
# Period COP (month / year / lifetime)
# =========================================================
class COPPeriodSensor(COPBaseSensor):
    def __init__(self, hass, period: str, language="en"):
        super().__init__(hass, language)
        self.period = period
        self.electric = PowerIntegrator()
        self.thermal = PowerIntegrator()
        self._attr_unique_id = f"cop_{period}"
        self._attributes = {}

    @property
    def name(self):
        return f"COP {self.period.capitalize()}"

    @property
    def extra_state_attributes(self):
        return self._attributes

    async def async_update(self):
        try:
            e = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_buitenunit_active_power").state)
            i = float(self.hass.states.get(
                "sensor.shelly_warmtepomp_binnenunit_active_power").state)
            electric = e + i
        except Exception:
            electric = 0

        thermal = None
        try:
            outlet = float(self.hass.states.get(
                "sensor.control_unit_water_outlet_temperature_2").state)
            inlet = float(self.hass.states.get(
                "sensor.control_unit_water_inlet_temperature_2").state)
            flow = float(self.hass.states.get(
                "sensor.control_unit_water_flow_2").state)
            thermal = abs(flow * (outlet - inlet) * 4.18 * 1000 / 3600)
        except Exception:
            pass

        self.electric.add(electric)
        if thermal is not None:
            self.thermal.add(thermal)

        if self.period == "lifetime":
            e_kwh = self.electric.energy_total()
            t_kwh = self.thermal.energy_total()
            self._state = t_kwh / e_kwh if e_kwh > 0 else None
            return

        if self.period == "month":
            e_map = self.electric.energy_by_month()
            t_map = self.thermal.energy_by_month()
        else:
            e_map = self.electric.energy_by_year()
            t_map = self.thermal.energy_by_year()

        result = {}
        for k in e_map:
            result[str(k)] = t_map.get(k, 0) / e_map[k] if e_map[k] > 0 else None

        self._attributes = result
        values = [v for v in result.values() if v is not None]
        self._state = sum(values) / len(values) if values else None


# =========================================================
# Setup
# =========================================================
async def async_setup_entry(hass, config_entry, async_add_entities):
    lang = config_entry.data.get("language", "en")
    pump_power = config_entry.data.get("circulation_pump_power", 59)

    sensors = [
        COPRealtimeSensor(hass, "heat", lang),
        COPRealtimeSensor(hass, "cool", lang),
        COPDHWSensor(hass, pump_power, lang),
        COPPeriodSensor(hass, "month", lang),
        COPPeriodSensor(hass, "year", lang),
        COPPeriodSensor(hass, "lifetime", lang),
    ]

    async_add_entities(sensors, True)
