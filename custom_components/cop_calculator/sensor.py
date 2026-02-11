from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_NAME
from datetime import datetime
from collections import defaultdict

class PowerIntegrator:
    def __init__(self):
        self.measurements = []
        self.start_time = None
        self.end_time = None

    def add_measurement(self, power, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now()
        self.measurements.append((timestamp, power))
        if self.start_time is None:
            self.start_time = timestamp
        self.end_time = timestamp

    def calculate_energy(self):
        if len(self.measurements) < 2:
            return 0

        total_energy_wh = 0
        for i in range(1, len(self.measurements)):
            time1, power1 = self.measurements[i-1]
            time2, power2 = self.measurements[i]
            time_diff = (time2 - time1).total_seconds() / 3600
            avg_power = (power1 + power2) / 2
            total_energy_wh += avg_power * time_diff

        total_energy_kwh = total_energy_wh / 1000
        return total_energy_kwh

    def get_measurements_by_month(self):
        measurements_by_month = defaultdict(list)
        for timestamp, power in self.measurements:
            month_key = (timestamp.year, timestamp.month)
            measurements_by_month[month_key].append((timestamp, power))
        return measurements_by_month

    def calculate_energy_by_month(self):
        measurements_by_month = self.get_measurements_by_month()
        energy_by_month = {}

        for month_key, month_measurements in measurements_by_month.items():
            if len(month_measurements) < 2:
                energy_by_month[month_key] = 0
                continue

            total_energy_wh = 0
            for i in range(1, len(month_measurements)):
                time1, power1 = month_measurements[i-1]
                time2, power2 = month_measurements[i]
                time_diff = (time2 - time1).total_seconds() / 3600
                avg_power = (power1 + power2) / 2
                total_energy_wh += avg_power * time_diff

            total_energy_kwh = total_energy_wh / 1000
            energy_by_month[month_key] = total_energy_kwh

        return energy_by_month

    def get_measurements_by_year(self):
        measurements_by_year = defaultdict(list)
        for timestamp, power in self.measurements:
            year_key = timestamp.year
            measurements_by_year[year_key].append((timestamp, power))
        return measurements_by_year

    def calculate_energy_by_year(self):
        measurements_by_year = self.get_measurements_by_year()
        energy_by_year = {}

        for year_key, year_measurements in measurements_by_year.items():
            if len(year_measurements) < 2:
                energy_by_year[year_key] = 0
                continue

            total_energy_wh = 0
            for i in range(1, len(year_measurements)):
                time1, power1 = year_measurements[i-1]
                time2, power2 = year_measurements[i]
                time_diff = (time2 - time1).total_seconds() / 3600
                avg_power = (power1 + power2) / 2
                total_energy_wh += avg_power * time_diff

            total_energy_kwh = total_energy_wh / 1000
            energy_by_year[year_key] = total_energy_kwh

        return energy_by_year

class RuimteverwarmingMonitor:
    def __init__(self):
        self.elektrisch_power_integrator = PowerIntegrator()
        self.thermisch_power_integrator = PowerIntegrator()
        self.is_actief = False

    def start_cyclus(self):
        self.elektrisch_power_integrator = PowerIntegrator()
        self.thermisch_power_integrator = PowerIntegrator()
        self.is_actief = True

    def update(self, elektrisch_vermogen, thermisch_vermogen):
        if self.is_actief:
            self.elektrisch_power_integrator.add_measurement(elektrisch_vermogen)
            self.thermisch_power_integrator.add_measurement(thermisch_vermogen)

    def eindig_cyclus(self, elektrisch_vermogen, thermisch_vermogen):
        self.update(elektrisch_vermogen, thermisch_vermogen)
        self.is_actief = False

    def bereken_elektrische_energie(self):
        return self.elektrisch_power_integrator.calculate_energy()

    def bereken_thermische_energie(self):
        return self.thermisch_power_integrator.calculate_energy()

class KoelingMonitor:
    def __init__(self):
        self.elektrisch_power_integrator = PowerIntegrator()
        self.thermisch_power_integrator = PowerIntegrator()
        self.is_actief = False

    def start_cyclus(self):
        self.elektrisch_power_integrator = PowerIntegrator()
        self.thermisch_power_integrator = PowerIntegrator()
        self.is_actief = True

    def update(self, elektrisch_vermogen, thermisch_vermogen):
        if self.is_actief:
            self.elektrisch_power_integrator.add_measurement(elektrisch_vermogen)
            self.thermisch_power_integrator.add_measurement(thermisch_vermogen)

    def eindig_cyclus(self, elektrisch_vermogen, thermisch_vermogen):
        self.update(elektrisch_vermogen, thermisch_vermogen)
        self.is_actief = False

    def bereken_elektrische_energie(self):
        return self.elektrisch_power_integrator.calculate_energy()

    def bereken_thermische_energie(self):
        return self.thermisch_power_integrator.calculate_energy()

class DHWMonitor:
    def __init__(self):
        self.start_temperatuur = None
        self.laagste_temperatuur = None
        self.eind_temperatuur = None
        self.totaal_temperatuurverschil = 0
        self.vorige_temperatuur = None
        self.is_actief = False
        self.start_tijd = None
        self.eind_tijd = None

    def start_cyclus(self, huidige_temperatuur):
        self.start_temperatuur = huidige_temperatuur
        self.laagste_temperatuur = huidige_temperatuur
        self.eind_temperatuur = None
        self.totaal_temperatuurverschil = 0
        self.vorige_temperatuur = huidige_temperatuur
        self.is_actief = True
        self.start_tijd = datetime.now()

    def update_temperatuur(self, huidige_temperatuur):
        if self.is_actief:
            if huidige_temperatuur < self.laagste_temperatuur:
                self.laagste_temperatuur = huidige_temperatuur

            if self.vorige_temperatuur is not None:
                verschil = huidige_temperatuur - self.vorige_temperatuur
                if verschil < 0:
                    self.totaal_temperatuurverschil += abs(verschil)

            self.vorige_temperatuur = huidige_temperatuur
            self.eind_temperatuur = huidige_temperatuur

    def eindig_cyclus(self, huidige_temperatuur):
        self.update_temperatuur(huidige_temperatuur)
        self.is_actief = False
        self.eind_tijd = datetime.now()

    def bereken_thermische_energie(self):
        if self.laagste_temperatuur is not None and self.eind_temperatuur is not None:
            volume = 260  # 260 liter DHW-vat
            totaal_verschil = (self.eind_temperatuur - self.laagste_temperatuur) + self.totaal_temperatuurverschil
            thermische_energie = (volume * totaal_verschil * 4.18) / 3600
            return thermische_energie
        else:
            return None

    def bereken_elektrische_energie(self, elektrisch_vermogen):
        if self.start_tijd and self.eind_tijd:
            tijdsduur_seconden = (self.eind_tijd - self.start_tijd).total_seconds()
            elektrisch_energie_wh = elektrisch_vermogen * (tijdsduur_seconden / 3600)
            elektrisch_energie_kwh = elektrisch_energie_wh / 1000
            return elektrisch_energie_kwh
        else:
            return None

class COPRuimteverwarmingSensor(Entity):
    def __init__(self, hass):
        self.hass = hass
        self.monitor = RuimteverwarmingMonitor()
        self._attr_unique_id = "cop_ruimteverwarming_sensor"
        self._state = None

    @property
    def name(self):
        return "COP Ruimteverwarming"

    @property
    def state(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"
        
    def update(self):
        modus = self.hass.states.get("sensor.control_unit_operation_state_2").state
        elektrisch_vermogen = self.bereken_elektrisch_vermogen()
        thermisch_vermogen = self.bereken_thermisch_vermogen()

        if modus == "operation_state_heat_thermo_on":
            if not self.monitor.is_actief:
                self.monitor.start_cyclus()
            self.monitor.update(elektrisch_vermogen, thermisch_vermogen)
        else:
            if self.monitor.is_actief:
                self.monitor.eindig_cyclus(elektrisch_vermogen, thermisch_vermogen)
                self._state = self.bereken_cop()

    def bereken_elektrisch_vermogen(self):
        buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
        binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
        return buitenunit_vermogen + binnenunit_vermogen

    def bereken_thermisch_vermogen(self):
        try:
            outlet_t = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_2").state)
            inlet_t = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_2").state)
            flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            delta_t = outlet_t - inlet_t
            thermisch_vermogen = (flow * delta_t * 4.18 * 1000) / 3600
            return abs(thermisch_vermogen)
        except (ValueError, AttributeError, TypeError):
            return None

    def bereken_cop(self):
        elektrisch_energie_kwh = self.monitor.bereken_elektrische_energie()
        thermisch_energie_kwh = self.monitor.bereken_thermische_energie()

        if elektrisch_energie_kwh is not None and thermisch_energie_kwh is not None and elektrisch_energie_kwh > 0:
            return thermisch_energie_kwh / elektrisch_energie_kwh
        else:
            return None

class COPKoelingSensor(Entity):
    def __init__(self, hass):
        self.hass = hass
        self.monitor = KoelingMonitor()
        self._attr_unique_id = "cop_koeling_sensor"
        self._state = None

    @property
    def name(self):
        return "COP Koeling"

    @property
    def state(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"
    
    def update(self):
        modus = self.hass.states.get("sensor.control_unit_operation_state_2").state
        elektrisch_vermogen = self.bereken_elektrisch_vermogen()
        thermisch_vermogen = self.bereken_thermisch_vermogen()

        if modus == "operation_state_cool_thermo_on":
            if not self.monitor.is_actief:
                self.monitor.start_cyclus()
            self.monitor.update(elektrisch_vermogen, thermisch_vermogen)
        else:
            if self.monitor.is_actief:
                self.monitor.eindig_cyclus(elektrisch_vermogen, thermisch_vermogen)
                self._state = self.bereken_cop()

    def bereken_elektrisch_vermogen(self):
        buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
        binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
        return buitenunit_vermogen + binnenunit_vermogen

    def bereken_thermisch_vermogen(self):
        try:
            outlet_t = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_koeling").state)
            inlet_t = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_koeling").state)
            flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
            delta_t = outlet_t - inlet_t
            thermisch_vermogen = (flow * delta_t * 4.18 * 1000) / 3600
            return abs(thermisch_vermogen)
        except (ValueError, AttributeError, TypeError):
            return None

    def bereken_cop(self):
        elektrisch_energie_kwh = self.monitor.bereken_elektrische_energie()
        thermisch_energie_kwh = self.monitor.bereken_thermische_energie()

        if elektrisch_energie_kwh is not None and thermisch_energie_kwh is not None and elektrisch_energie_kwh > 0:
            return thermisch_energie_kwh / elektrisch_energie_kwh
        else:
            return None

class COPDHWSensor(Entity):
    def __init__(self, hass):
        self.hass = hass
        self.dhw_monitor = DHWMonitor()
        self._attr_unique_id = "cop_dhw_sensor"
        self._state = None

    @property
    def name(self):
        return "COP DHW"

    @property
    def state(self):
        return self._state

     @property
    def native_unit_of_measurement(self):
        return "COP"

    @property
    def icon(self):
        return "mdi:calculator"

    def update(self):
        modus = self.hass.states.get("sensor.control_unit_operation_state_2").state
        huidige_temperatuur = float(self.hass.states.get("sensor.dhw_current_temperature").state)
        elektrisch_vermogen = self.bereken_elektrisch_vermogen_dhw()

        if modus == "operation_state_dhw_on":
            if not self.dhw_monitor.is_actief:
                self.dhw_monitor.start_cyclus(huidige_temperatuur)
            else:
                self.dhw_monitor.update_temperatuur(huidige_temperatuur)
        else:
            if self.dhw_monitor.is_actief:
                self.dhw_monitor.eindig_cyclus(huidige_temperatuur)
                elektrisch_energie_kwh = self.dhw_monitor.bereken_elektrische_energie(elektrisch_vermogen)
                thermisch_energie_kwh = self.dhw_monitor.bereken_thermische_energie()
                if elektrisch_energie_kwh is not None and thermisch_energie_kwh is not None and elektrisch_energie_kwh > 0:
                    self._state = thermisch_energie_kwh / elektrisch_energie_kwh
                else:
                    self._state = None

    def bereken_elektrisch_vermogen_dhw(self):
        buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
        binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
        pomp_vermogen = 59
        elektrisch_element_vermogen = binnenunit_vermogen - pomp_vermogen
        return buitenunit_vermogen + elektrisch_element_vermogen

class COPPeriodeSensor(Entity):
    def __init__(self, hass, modus, periode_type):
        self.hass = hass
        self.modus = modus
        self.periode_type = periode_type  # "maand", "jaar", of "lifetime"
        self.elektrisch_power_integrator = PowerIntegrator()
        self.thermisch_power_integrator = PowerIntegrator()
        self._state = None
        self._attributes = {}
        self._attr_unique_id = f"cop_{modus}_{periode_type}_sensor"
        
    @property
    def name(self):
        return f"COP {self.modus} {self.periode_type}"

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
        # Haal het huidige elektrisch vermogen op
        elektrisch_vermogen = self.bereken_elektrisch_vermogen()

        # Haal het huidige thermisch vermogen op
        thermisch_vermogen = self.bereken_thermisch_vermogen()

        # Voeg de metingen toe aan de power integrators
        self.elektrisch_power_integrator.add_measurement(elektrisch_vermogen)
        if thermisch_vermogen is not None:
            self.thermisch_power_integrator.add_measurement(thermisch_vermogen)

        # Bereken de COP per periode
        if self.periode_type == "maand":
            cop_per_periode = self.bereken_cop_per_maand()
            self._attributes = cop_per_periode
            # Bereken het gemiddelde COP over de maanden
            cop_waarden = [cop for cop in cop_per_periode.values() if cop is not None]
            if cop_waarden:
                self._state = sum(cop_waarden) / len(cop_waarden)
            else:
                self._state = None
        elif self.periode_type == "jaar":
            cop_per_periode = self.bereken_cop_per_jaar()
            self._attributes = cop_per_periode
            # Bereken het gemiddelde COP over de jaren
            cop_waarden = [cop for cop in cop_per_periode.values() if cop is not None]
            if cop_waarden:
                self._state = sum(cop_waarden) / len(cop_waarden)
            else:
                self._state = None
        elif self.periode_type == "lifetime":
            self._state = self.bereken_lifetime_cop()

    def bereken_cop_per_maand(self):
        elektrisch_energie_per_maand = self.elektrisch_power_integrator.calculate_energy_by_month()
        thermisch_energie_per_maand = self.thermisch_power_integrator.calculate_energy_by_month()

        cop_per_maand = {}
        for month_key in elektrisch_energie_per_maand:
            elektrisch_energie = elektrisch_energie_per_maand[month_key]
            thermisch_energie = thermisch_energie_per_maand.get(month_key, 0)

            if elektrisch_energie > 0:
                cop_per_maand[month_key] = thermisch_energie / elektrisch_energie
            else:
                cop_per_maand[month_key] = None

        return cop_per_maand

    def bereken_cop_per_jaar(self):
        elektrisch_energie_per_jaar = self.elektrisch_power_integrator.calculate_energy_by_year()
        thermisch_energie_per_jaar = self.thermisch_power_integrator.calculate_energy_by_year()

        cop_per_jaar = {}
        for year_key in elektrisch_energie_per_jaar:
            elektrisch_energie = elektrisch_energie_per_jaar[year_key]
            thermisch_energie = thermisch_energie_per_jaar.get(year_key, 0)

            if elektrisch_energie > 0:
                cop_per_jaar[year_key] = thermisch_energie / elektrisch_energie
            else:
                cop_per_jaar[year_key] = None

        return cop_per_jaar

    def bereken_lifetime_cop(self):
        totale_elektrische_energie = self.elektrisch_power_integrator.calculate_energy()
        totale_thermische_energie = self.thermisch_power_integrator.calculate_energy()

        if totale_elektrische_energie > 0:
            return totale_thermische_energie / totale_elektrische_energie
        else:
            return None

    def bereken_elektrisch_vermogen(self):
        if self.modus == "operation_state_heat_thermo_on":
            buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            return buitenunit_vermogen + binnenunit_vermogen
        elif self.modus == "operation_state_cool_thermo_on":
            buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            return buitenunit_vermogen + binnenunit_vermogen
        elif self.modus == "operation_state_dhw_on":
            buitenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_buitenunit_active_power").state)
            binnenunit_vermogen = float(self.hass.states.get("sensor.shelly_warmtepomp_binnenunit_active_power").state)
            pomp_vermogen = 59
            elektrisch_element_vermogen = binnenunit_vermogen - pomp_vermogen
            return buitenunit_vermogen + elektrisch_element_vermogen
        else:
            return 0

    def bereken_thermisch_vermogen(self):
        if self.modus == "operation_state_heat_thermo_on":
            try:
                outlet_t = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_2").state)
                inlet_t = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_2").state)
                flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
                delta_t = outlet_t - inlet_t
                thermisch_vermogen = (flow * delta_t * 4.18 * 1000) / 3600
                return abs(thermisch_vermogen)
            except (ValueError, AttributeError, TypeError):
                return None
        elif self.modus == "operation_state_cool_thermo_on":
            try:
                outlet_t = float(self.hass.states.get("sensor.control_unit_water_outlet_temperature_koeling").state)
                inlet_t = float(self.hass.states.get("sensor.control_unit_water_inlet_temperature_koeling").state)
                flow = float(self.hass.states.get("sensor.control_unit_water_flow_2").state)
                delta_t = outlet_t - inlet_t
                thermisch_vermogen = (flow * delta_t * 4.18 * 1000) / 3600
                return abs(thermisch_vermogen)
            except (ValueError, AttributeError, TypeError):
                return None
        elif self.modus == "operation_state_dhw_on":
            return None  # Thermische energie voor DHW wordt apart berekend
        else:
            return None

async def async_setup_entry(hass, config_entry, async_add_entities):
    huidige_jaar = datetime.now().year
    sensors = [
        COPRuimteverwarmingSensor(hass),
        COPKoelingSensor(hass),
        COPDHWSensor(hass),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "maand"),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "jaar"),
        COPPeriodeSensor(hass, "operation_state_heat_thermo_on", "lifetime"),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "maand"),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "jaar"),
        COPPeriodeSensor(hass, "operation_state_cool_thermo_on", "lifetime"),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "maand"),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "jaar"),
        COPPeriodeSensor(hass, "operation_state_dhw_on", "lifetime")
    ]
    async_add_entities(sensors, True)

