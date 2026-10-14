import appdaemon.plugins.hass.hassapi as hass
import datetime
import math
import time


class EMSVerwarming(hass.Hass):
    MINIMALE_WISSELTIJD_SECONDEN = 1200
    MODBUS_HASS_TIMEOUT = 5
    MODBUS_TIMEOUT = 10
    MODBUS_RETRIES = 3

    def initialize(self):
        self.log("--- EMS Verwarming Gestart (Volledige Regie + Modbus + Beveiligingen) ---")

        # Exacte entiteitnaam van je Generic Thermostat:
        self.thermo_entity = "climate.woonkamer"

        # Sensor voor de werkelijke status van de warmtepomp
        self.wp_status_sensor = "sensor.control_unit_operation_state_2"
        self.wp_status_aan_waarde = "operation_state_heat_thermo_on"
        self.forecast_entity = "sensor.energie_core_forecast"
        self.vrijgave_helper = "input_boolean.verwarming_vrijgave"

        self.listen_state(self._wp_status_changed, self.wp_status_sensor)

        # Draai elke 15 minuten, synchroon met de kwartiertarieven
        self.run_every(self.simuleer_behoefte, "now", 15 * 60)

    def _get_float_state(self, entity, default, attribute=None):
        value = self.get_state(entity, attribute=attribute)
        try:
            result = float(value)
        except (TypeError, ValueError):
            self.log(
                f"Sensor {entity} bevat geen bruikbare numerieke waarde "
                f"({value!r}); fallback {default} gebruikt.",
                level="WARNING",
            )
            return default

        if not math.isfinite(result):
            self.log(
                f"Sensor {entity} bevat een niet-eindige waarde "
                f"({value!r}); fallback {default} gebruikt.",
                level="WARNING",
            )
            return default
        return result

    def _wp_status_changed(self, entity, _attribute, _old, new, _kwargs):
        if new in (None, "unknown", "unavailable"):
            self.log(f"Statussensor {entity} heeft ongeldige status {new!r}.", level="WARNING")
            return
        self._save_switch_tracking(new == self.wp_status_aan_waarde, self.datetime(aware=True))

    def _save_switch_tracking(self, is_on, changed_at, registered_setpoint=None):
        attributes = {
            "last_switch_at": changed_at.isoformat(),
            "geregistreerd_setpoint": registered_setpoint,
        }
        if registered_setpoint is None:
            attributes.pop("geregistreerd_setpoint")
            existing_setpoint = self.get_state(
                self.vrijgave_helper, attribute="geregistreerd_setpoint"
            )
            if existing_setpoint is not None:
                attributes["geregistreerd_setpoint"] = existing_setpoint
        self.set_state(
            self.vrijgave_helper,
            state="on" if is_on else "off",
            attributes=attributes,
        )

    def _last_switch_seconds(self, now, physical_state):
        helper_state = self.get_state(self.vrijgave_helper)
        if helper_state not in ("on", "off"):
            helper_state = "on" if physical_state else "off"
            self.log(
                f"{self.vrijgave_helper} heeft geen geldige status; "
                "de pendelbeveiliging start conservatief vanaf nu.",
                level="WARNING",
            )
            self._save_switch_tracking(helper_state == "on", now)

        last_switch = self.get_state(self.vrijgave_helper, attribute="last_switch_at")
        try:
            if not last_switch:
                raise ValueError("tijdstip ontbreekt")
            changed_at = self.parse_datetime(last_switch)
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=datetime.timezone.utc)
            seconds = (now - changed_at).total_seconds()
            if seconds < 0:
                raise ValueError("tijdstip ligt in de toekomst")
            return helper_state == "on", seconds
        except (TypeError, ValueError) as err:
            self.log(
                f"Geen geldige schakeltijd opgeslagen in {self.vrijgave_helper}: {err}. "
                "De pendelbeveiliging start conservatief vanaf nu.",
                level="WARNING",
            )
            self._save_switch_tracking(helper_state == "on", now)
            return helper_state == "on", 0

    def simuleer_behoefte(self, _kwargs):
        # Thermische eigenschappen van het huis
        k_afkoel = self._get_float_state("sensor.huis_afkoel_constante", 0.015)
        k_solar = self._get_float_state("sensor.huis_solar_gain_constante", 0.48)
        k_opwarm = self._get_float_state("sensor.huis_opwarm_constante", 1.15)
        if k_opwarm <= 0:
            self.log(
                "Sensor sensor.huis_opwarm_constante moet groter zijn dan 0; "
                "fallback 1.15 gebruikt.",
                level="WARNING",
            )
            k_opwarm = 1.15

        # Actuele sensoren en stooklijn
        t_binnen = self._get_float_state("sensor.woonkamer_temperatuur_gemiddeld", 19.0)
        zon_nu = self._get_float_state("sensor.solaredge_i1_ac_power", 0.0)
        t_buiten = self._get_float_state("sensor.kantoor_buiten_temperatuur", 10.0)
        stooklijn_setpoint = int(self._get_float_state("sensor.warmtepomp_stooklijn", 20.0))

        # Controleer de werkelijke fysieke status van de Hitachi via jouw sensor
        wp_modus = self.get_state(self.thermo_entity)
        if wp_modus == "off":
            self.log("STOP: De thermostaat staat handmatig UIT. EMS doet niets.")
            #self.stuur_warmtepomp(False, 20)
            return

        target_temp = self._get_float_state(
            self.thermo_entity, 19.5, attribute="temperature"
        )
        t_target_min = target_temp - 0.4

        # --- HARD LIMITS (Voorkom dat het te warm wordt) ---
        if t_binnen >= (target_temp + 1.0) or t_binnen >= 21.5:
            self.log(f"HARD CRITIEK UIT: Huis is te warm ({t_binnen}°C). Limiet van {target_temp + 1.0}°C bereikt.")
            self.stuur_warmtepomp(False, 20)
            return

        # --- COMFORT OVERRIDES (Te koud) ---
        if t_binnen < 18.0 or (target_temp - t_binnen > 0.8):
            self.log(f"FORCE AAN: Comfort-override activeert. T_binnen ({t_binnen}) is te laag.")
            self.stuur_warmtepomp(True, stooklijn_setpoint)
            return

        # Forecastmatrix levert zowel de PV-verwachting als het all-in tarief.
        forecast_state = self.get_state(self.forecast_entity)
        forecast = self.get_state(self.forecast_entity, attribute="forecast")
        if (
            forecast_state in (None, "unknown", "unavailable")
            or not isinstance(forecast, list)
            or not forecast
        ):
            self.log(
                f"DATA ERROR: {self.forecast_entity} is niet beschikbaar "
                "of bevat geen forecastmatrix. "
                "Fallback op basisregeling.",
                level="WARNING",
            )
            self.stuur_warmtepomp(t_binnen < t_target_min, stooklijn_setpoint)
            return

        nu = self.datetime(aware=True)
        huidige_prijs = self.get_forecast_value(forecast, nu, "net_prijs", 15)
        if huidige_prijs is None:
            self.log(
                f"DATA ERROR: Geen bruikbaar huidig tarief in {self.forecast_entity}. "
                "Fallback op basisregeling.",
                level="WARNING",
            )
            self.stuur_warmtepomp(t_binnen < t_target_min, stooklijn_setpoint)
            return

        # 3. Bereken benodigde stooktijd
        graden_te_overbruggen = max(0.1, target_temp - t_binnen)
        kwartieren_stooktijd = int(math.ceil((graden_te_overbruggen / k_opwarm) * 4))
        kwartieren_stooktijd = max(1, kwartieren_stooktijd)

        # 4. De Thermische & Financiële Simulatie (4 uur vooruitkijken)
        sim_t = t_binnen
        stoken_nodig_vanaf_q = None
        laagste_gem_prijs = 999.0
        moment_start_stoken = 0
        horizon_kwartieren = 16

        for q in range(0, horizon_kwartieren + 1):
            doel_tijd = nu + datetime.timedelta(minutes=q * 15)

            zon_q = self.get_forecast_value(forecast, doel_tijd, "pv_kw", 15) or 0.0
            sim_t = sim_t - ((sim_t - t_buiten) * k_afkoel * 0.25) + (zon_q * k_solar * 0.25)

            if sim_t < t_target_min and stoken_nodig_vanaf_q is None:
                stoken_nodig_vanaf_q = q

            totaal_prijs_sessie = 0
            geldige_kwartieren = 0

            for s in range(q, q + kwartieren_stooktijd):
                tijd_s = nu + datetime.timedelta(minutes=s * 15)
                prijs_s = self.get_forecast_value(forecast, tijd_s, "net_prijs", 15)
                totaal_prijs_sessie += huidige_prijs if prijs_s is None else prijs_s
                geldige_kwartieren += 1

            gem_prijs_sessie = totaal_prijs_sessie / geldige_kwartieren
            if gem_prijs_sessie < laagste_gem_prijs:
                laagste_gem_prijs = gem_prijs_sessie
                moment_start_stoken = q

        # 5. De Beslissing voor NU
        zon_lockout = zon_nu > 500
        prijs_nu_all_in = huidige_prijs

        # STRATEGIE A: Er is een warmtebehoefte voorspeld binnen 4 uur
        if stoken_nodig_vanaf_q is not None:
            totaal_prijs_nu = 0
            for s in range(0, kwartieren_stooktijd):
                tijd_s = nu + datetime.timedelta(minutes=s * 15)
                prijs_s = self.get_forecast_value(forecast, tijd_s, "net_prijs", 15)
                totaal_prijs_nu += huidige_prijs if prijs_s is None else prijs_s
            gem_prijs_nu = totaal_prijs_nu / kwartieren_stooktijd

            if zon_lockout:
                self.log(f"UIT: Noodzaak over {stoken_nodig_vanaf_q*15}m, maar actuele zon blockt ({int(zon_nu)}W).")
                self.stuur_warmtepomp(False, 20)
            elif gem_prijs_nu <= laagste_gem_prijs + 0.005:
                self.log(f"AAN: Noodzaak over {stoken_nodig_vanaf_q*15}m. Prijs nu ({round(gem_prijs_nu,3)}) is optimaal.")
                self.stuur_warmtepomp(True, stooklijn_setpoint)
            else:
                self.log(f"WACHTEN: Prijs nu ({round(gem_prijs_nu,3)}) > straks ({round(laagste_gem_prijs,3)}) bij t+{moment_start_stoken*15}m.")
                self.stuur_warmtepomp(False, 20)

        # STRATEGIE B: OPPORTUNISTISCH VOORVERWARMEN (Bufferen)
        else:
            if (prijs_nu_all_in < 0.14 or zon_nu > 1500) and t_binnen < (target_temp + 0.5):
                self.log(f"AAN (Bufferen): Prijs is laag ({round(prijs_nu_all_in,3)}) of zon schijnt. Vloer laden. T_binnen: {t_binnen}°C")
                self.stuur_warmtepomp(True, stooklijn_setpoint)
            else:
                # FIX: Log level verlaagd naar DEBUG om overstroming van je reguliere logbestanden te voorkomen
                self.log(f"IDLE: Geen noodzaak en geen gunstige buffercondities. T_binnen: {round(t_binnen,2)}°C.", level="DEBUG")
                self.stuur_warmtepomp(False, 20)

    def stuur_warmtepomp(self, state_wensen, setpoint_wensen):
        nu = self.datetime(aware=True)
        current_state_raw = self.get_state(self.wp_status_sensor)
        if current_state_raw in (None, "unknown", "unavailable"):
            self.log(
                f"Geen betrouwbare status van {self.wp_status_sensor}; "
                "Modbus-aansturing overgeslagen.",
                level="ERROR",
            )
            return
        wp_actueel_aan = current_state_raw == self.wp_status_aan_waarde
        helper_is_on, seconds_in_state = self._last_switch_seconds(nu, wp_actueel_aan)
        new_state_bool = bool(state_wensen)

        # --- SLIMMERE PENDELBEVEILIGING ---
        if helper_is_on and not new_state_bool:
            if seconds_in_state < self.MINIMALE_WISSELTIJD_SECONDEN:
                self.log(f"PENDEL-REMEDIATIE: Script wilde uitschakelen, maar minimale looptijd (20m) is niet bereikt. Pomp blijft AAN. (Draait nu: {int(seconds_in_state/60)}m)")
                new_state_bool = True
                setpoint_wensen = int(self._get_float_state("sensor.warmtepomp_stooklijn", 20.0))

        elif not helper_is_on and new_state_bool:
            if seconds_in_state < self.MINIMALE_WISSELTIJD_SECONDEN:
                self.log(f"PENDEL-REMEDIATIE: Script wilde inschakelen, maar minimale rusttijd (20m) is nog actief. Pomp blijft UIT. (Nu in rust: {int(seconds_in_state/60)}m)")
                new_state_bool = False

        # --- COMMUNICATIE MET MODBUS ---
        helper_state = "on" if new_state_bool else "off"
        vorig_setpoint_raw = self.get_state(
            self.vrijgave_helper, attribute="geregistreerd_setpoint"
        )
        try:
            vorig_setpoint = int(float(vorig_setpoint_raw))
        except (TypeError, ValueError):
            vorig_setpoint = 0
        setpoint = int(setpoint_wensen) if new_state_bool else 20

        try:
            if new_state_bool:
                if vorig_setpoint != setpoint:
                    self.log(f"MODBUS: Schrijf Register 1005 = {setpoint}°C")
                    self._write_modbus_register(1005, setpoint)
                if not wp_actueel_aan:
                    self.log("MODBUS: Schrijf Register 1002 = 1 (AAN)")
                    self._write_modbus_register(1002, 1)
            else:
                if wp_actueel_aan:
                    self.log("MODBUS: Schrijf Register 1002 = 0 (UIT)")
                    self._write_modbus_register(1002, 0)
                if vorig_setpoint != 20:
                    self.log("MODBUS: Schrijf Register 1005 = 20°C")
                    self._write_modbus_register(1005, 20)
        except Exception:
            self.error("Modbus-aansturing van de warmtepomp is mislukt.")
            raise

        if self.get_state(self.vrijgave_helper) != helper_state:
            self._save_switch_tracking(new_state_bool, nu, setpoint)
        else:
            self.set_state(
                self.vrijgave_helper,
                attributes={"geregistreerd_setpoint": setpoint},
            )

        self.run_in(self._verify_warmtepomp_status, 15, expected_on=new_state_bool)

    def _write_modbus_register(self, address, value):
        for attempt in range(1, self.MODBUS_RETRIES + 1):
            try:
                self.call_service(
                    "modbus/write_register",
                    hub="atw-mbs-02",
                    address=address,
                    slave=1,
                    value=value,
                    hass_timeout=self.MODBUS_HASS_TIMEOUT,
                    timeout=self.MODBUS_TIMEOUT,
                )
                return
            except Exception as err:
                self.log(
                    f"Modbus-write register {address} poging {attempt}/"
                    f"{self.MODBUS_RETRIES} mislukt: {err}",
                    level="WARNING",
                )
                if attempt == self.MODBUS_RETRIES:
                    raise
                time.sleep(0.5 * attempt)

    def _verify_warmtepomp_status(self, kwargs):
        expected_on = kwargs["expected_on"]
        current_status = self.get_state(self.wp_status_sensor)
        if current_status in (None, "unknown", "unavailable"):
            self.log(
                f"Modbus-statuscontrole niet mogelijk: {self.wp_status_sensor} "
                f"is {current_status!r}.",
                level="WARNING",
            )
            return
        actual_on = current_status == self.wp_status_aan_waarde
        if actual_on != expected_on:
            self.log(
                "Modbus-statuscontrole mislukt: "
                f"verwacht {'AAN' if expected_on else 'UIT'}, "
                f"gemeten {'AAN' if actual_on else 'UIT'}.",
                level="WARNING",
            )

    def get_forecast_value(self, data_list, target_dt, key, window_minutes):
        for item in data_list:
            if not isinstance(item, dict):
                continue
            time_value = item.get("tijd")
            if not time_value:
                continue
            try:
                period_start = (
                    self.parse_datetime(time_value)
                    if isinstance(time_value, str)
                    else time_value
                )
                if not isinstance(period_start, datetime.datetime):
                    continue
                if period_start.tzinfo is None:
                    period_start = period_start.replace(tzinfo=datetime.timezone.utc)
                value = float(item[key])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            if period_start <= target_dt < (
                period_start + datetime.timedelta(minutes=window_minutes)
            ):
                return value
        return None
