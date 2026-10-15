import appdaemon.plugins.hass.hassapi as hass
import datetime
import math

class WarmtepompKoelStabilisator(hass.Hass):

    def initialize(self):
        # Entiteiten en instellingen
        self.sensor_t_binnen = "sensor.woonkamer_temperatuur_gemiddeld"
        self.sensor_rv_binnen = "sensor.0x54ef44100128055b_humidity"
        self.sensor_outlet = "sensor.control_unit_water_outlet_temperature_2" # Aanvoerwater naar de vloer
        self.sensor_state = "sensor.control_unit_operation_state_2" # Actieve status van de Hitachi
        self.sensor_corrected_target = "sensor.control_unit_corrected_target_temperature_2" 
        
        # Instellingen
        self.min_water_temp = 18.0
        self.start_water_temp = 20.0 # Veilige, warme starttemperatuur
        
        # Interne status onthouden
        self.huidige_koel_target = self.start_water_temp
        self.last_adjustment_time = datetime.datetime.now()
        self.was_cooling_mode = False
        self.was_thermo_on = False 

        # Run de controle ELKE MINUUT
        self.run_minutely(self.controleer_koeling, start=None)

    def controleer_koeling(self, kwargs):
        try:
            # 1. STATUS UITLEZEN
            current_state = self.get_state(self.sensor_state)

            # Beveiliging: Als de status onbekend is (HA herstart), doe niks en wacht
            if current_state in [None, "unavailable", "unknown"]:
                return

            # 2. CHECK OF DE WP IN EEN KOELMODUS STAAT
            is_in_koelmodus = "cool" in current_state.lower()

            # RESET LOGICA: Als de warmtepomp NIET (meer) in koelmodus staat
            if not is_in_koelmodus:
                if self.was_cooling_mode:
                    self.huidige_koel_target = self.start_water_temp
                    self.was_cooling_mode = False
                    self.was_thermo_on = False
                    self.log("Warmtepomp is uit de koelmodus gegaan. Script stand-by.")
                return

            self.was_cooling_mode = True

            # 3. SENSOREN UITLEZEN + VALIDATIE
            t_binnen = self._get_safe_float(self.sensor_t_binnen, default=25.0)
            rv_binnen = self._get_safe_float(self.sensor_rv_binnen, default=55.0)
            t_outlet = self._get_safe_float(self.sensor_outlet, default=20.0)

            # NIEUW: Lees de werkelijke gecorrigeerde doeltemperatuur live uit
            actuele_corrected_target = self._get_safe_float(self.sensor_corrected_target, default=20.0)

            # 4. DAUWPUNT EN GRENZEN BEREKENEN
            t_dauwpunt = self._calculate_dew_point(t_binnen, rv_binnen)
            t_water_min_veilig = t_dauwpunt + 1.5
            target_ondergrens = max(int(self.min_water_temp), math.ceil(t_water_min_veilig))

            nu = datetime.datetime.now()
            tijd_sinds_aanpassing = max(0.0, (nu - self.last_adjustment_time).total_seconds())

            # 5. BORGING VAN DE RUSTFASE (STAND-BY CHECK)
            # Als de status NIET exact thermo_on is, dwingen we stand-by op basis van de ECHTE sensor!
            if current_state != "operation_state_cool_thermo_on":
                
                # Check of de echte sensor (gele lijn) onder de 20°C staat
                if actuele_corrected_target < self.start_water_temp or self.was_thermo_on:
                    # Stuur altijd de 20°C naar Modbus
                    self._schrijf_modbus_target(self.start_water_temp, forceer=True)
                    self.huidige_koel_target = self.start_water_temp
                    
                    # Log alleen de 'Borging' als de machine er in rust stiekem zélf vandoor ging
                    if not self.was_thermo_on:
                        self.log(f"STAND-BY BORGING: Gecorrigeerde doeltemperatuur week af ({actuele_corrected_target}°C). Target hard hersteld naar 20°C.")
                    else:
                        self.log("Compressor gestopt. Target stand-by gezet op 20°C.")
            
                # Reset de status-variabele en de timer continu zolang de compressor slaapt
                self.was_thermo_on = False
                self.last_adjustment_time = nu
                return # Breek hier direct af, ga in rust nooit verder naar de actieve run-logica

            # --- VANAF HIER ZITTEN WE IN EEN ACTIEVE RUN ('operation_state_cool_thermo_on') ---

            # Log de actieve status van de run voor debugging
            self.log(f"Status: {current_state} | Dauwpunt: {t_dauwpunt:.1f}°C | Ondergrens: {target_ondergrens}°C | Outlet: {t_outlet:.1f}°C | Target: {self.huidige_koel_target}°C | Timer: {tijd_sinds_aanpassing:.0f}s/180s")

            # OPSTART-BOOST: Als de compressor NET is aangesprongen (vorige minuut nog niet),
            # schrijf dan DIRECT de 20°C naar Modbus zonder de timer af te wachten!
            if not self.was_thermo_on:
                self.log(f"Compressor START gedetecteerd! Schrijf DIRECT {self.start_water_temp}°C naar Modbus.")
                self._schrijf_modbus_target(self.start_water_temp, forceer=True)
                self.huidige_koel_target = self.start_water_temp
                self.last_adjustment_time = nu
                self.was_thermo_on = True
                return

            # GELEIDELIJK ZAKKEN (3 minuten +/- 5s marge om AppDaemon timing-fouten op te vangen)
            if self.huidige_koel_target > target_ondergrens:
                if tijd_sinds_aanpassing >= 175: 
                    nieuwe_target = self.huidige_koel_target - 1
                    self._schrijf_modbus_target(nieuwe_target)
                    self.huidige_koel_target = nieuwe_target
                    self.last_adjustment_time = nu
                    self.log(f"STABIEL: Run loopt goed. Target stapje verlaagd naar {nieuwe_target}°C.")

        except Exception as e:
            self.log(f"Fout in koelregeling: {e}", level="ERROR")

    def _schrijf_modbus_target(self, target_waarde, forceer=False):
        """Schrijft naar Modbus (bescherming tegen dubbele opdrachten)"""
        self.call_service(
            "modbus/write_register",
            hub="atw-mbs-02",
            address=1006,
            slave=1,
            value=int(target_waarde)
        )

    def _get_safe_float(self, entity, default):
        state = self.get_state(entity)
        try:
            if state is not None and state not in ["unavailable", "unknown"]:
                return float(state)
        except ValueError:
            pass
        return float(default)

    def _calculate_dew_point(self, t, rh):
        if rh <= 0 or rh >= 100:
            return t - 5
        gamma = math.log(rh / 100.0) + ((17.67 * t) / (243.5 + t))
        return (243.5 * gamma) / (17.67 - gamma)