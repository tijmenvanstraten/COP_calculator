import appdaemon.plugins.hass.hassapi as hass
import datetime
import math
import time


class EMSVerwarming(hass.Hass):
    MINIMALE_WISSELTIJD_SECONDEN = 1200
    MODBUS_HASS_TIMEOUT = 5
    MODBUS_TIMEOUT = 10
    MODBUS_RETRIES = 3
    STATUS_VERIFY_INTERVAL_SECONDS = 10
    STATUS_VERIFY_MAX_ATTEMPTS = 3
    FALLBACK_K_AFKOEL = 0.015
    FALLBACK_K_SOLAR = 0.48
    FALLBACK_K_OPWARM = 1.15
    FALLBACK_K_WIND = 0.0
    WIND_SPEED_ENTITY = "sensor.thuis_windsnelheid"
    WIND_LOSS_FACTOR_ENTITY = "sensor.huis_wind_loss_factor"
    BUFFER_PRICE_THRESHOLD_EUR = 0.14
    PRICE_WINDOW_TOLERANCE_EUR = 0.005
    PRICE_HYSTERESIS_EUR = 0.005
    PV_BUFFER_POWER_W = 1500
    BUFFER_TEMPERATURE_MARGIN_C = 0.5
    SIMULATION_QUARTER_MINUTES = 15
    SIMULATION_HORIZON_QUARTERS = 16
    MAX_MODEL_HEATING_QUARTERS = 96
    MODEL_DAY_START_HOUR = 7
    MODEL_DAY_END_HOUR = 23
    FORECAST_VALUE_KEYS = ("pv_kw", "net_prijs")
    WP_STATUS_AAN = "operation_state_heat_thermo_on"
    FORECAST_ENTITY = "sensor.energie_core_forecast"
    VRIJGAVE_HELPER = "input_boolean.verwarming_vrijgave"
    DIAGNOSTIC_ENTITY = "sensor.ems_verwarming_diagnostiek"
    THERMAL_MODEL_QUALITY_ENTITY = "sensor.huis_model_kwaliteit"
    THERMAL_MODEL_GAIN_ENTITY = "sensor.huis_verwarming_gain"
    THERMAL_MODEL_INTERNAL_GAIN_ENTITY = "sensor.huis_interne_winst"

    def initialize(self):
        self.log("--- EMS Verwarming Gestart (Volledige Regie + Modbus + Beveiligingen) ---")

        # Exacte entiteitnaam van je Generic Thermostat:
        self.thermo_entity = "climate.woonkamer"

        # Sensor voor de werkelijke status van de warmtepomp
        self.wp_status_sensor = "sensor.control_unit_operation_state_2"
        self.wp_status_aan_waarde = self.WP_STATUS_AAN
        self.forecast_entity = self.FORECAST_ENTITY
        self.vrijgave_helper = self.VRIJGAVE_HELPER

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

    def _publish_diagnostic(self, decision, reason, attributes=None):
        diagnostic_attributes = {
            "friendly_name": "EMS verwarming diagnostiek",
            "icon": "mdi:heat-pump",
            "decision": decision,
            "reason": reason,
            "updated_at": self.datetime(aware=True).isoformat(),
        }
        if attributes:
            diagnostic_attributes.update(attributes)
        try:
            self.set_state(
                self.DIAGNOSTIC_ENTITY,
                state=decision,
                attributes=diagnostic_attributes,
            )
        except Exception as err:
            self.log(
                f"Publiceren van {self.DIAGNOSTIC_ENTITY} mislukt: {err}",
                level="ERROR",
            )

    def _decide_and_control(
        self, decision, reason, state_wanted, setpoint, diagnostics
    ):
        control_ok = self.stuur_warmtepomp(state_wanted, setpoint)
        self._publish_diagnostic(
            decision, reason, {**diagnostics, "modbus_command_ok": control_ok}
        )

    def _validate_forecast(self, forecast):
        if not isinstance(forecast, list):
            self.log("Forecastmatrix heeft geen lijststructuur.", level="WARNING")
            return []

        valid_rows = []
        invalid_rows = 0
        for row in forecast:
            if not isinstance(row, dict) or not row.get("tijd"):
                invalid_rows += 1
                continue
            try:
                time_value = row["tijd"]
                period_start = (
                    self.parse_datetime(time_value)
                    if isinstance(time_value, str)
                    else time_value
                )
                if not isinstance(period_start, datetime.datetime):
                    raise ValueError("tijd is geen datetime")
                if period_start.tzinfo is None:
                    period_start = period_start.replace(tzinfo=datetime.timezone.utc)
                values = {
                    key: float(row[key])
                    for key in self.FORECAST_VALUE_KEYS
                }
                if not all(math.isfinite(value) for value in values.values()):
                    raise ValueError("forecast bevat een niet-eindige waarde")
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid_rows += 1
                continue
            valid_rows.append({"tijd": period_start, **values})

        if invalid_rows:
            self.log(
                f"Forecastmatrix bevat {invalid_rows} ongeldige rij(en); "
                f"{len(valid_rows)} bruikbare rij(en) behouden.",
                level="WARNING",
            )
        return valid_rows

    def _simulate_temperature_step(
        self, indoor_temp, outdoor_temp, pv_kw, k_afkoel, k_solar, k_wind, wind_speed
    ):
        # k_wind is per km/h; met windsnelheid wordt dit een afkoelfactor per uur.
        loss_rate = k_afkoel + k_wind * wind_speed
        step_hours = self.SIMULATION_QUARTER_MINUTES / 60
        return (
            indoor_temp
            - ((indoor_temp - outdoor_temp) * loss_rate * step_hours)
            + (pv_kw * k_solar * step_hours)
        )

    def _model_float(self, entity, attribute=None):
        value = self.get_state(entity, attribute=attribute)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def _load_thermal_model(self):
        quality = self.get_state(self.THERMAL_MODEL_QUALITY_ENTITY)
        if quality != "ok":
            return None, f"modelkwaliteit is {quality!r}"

        model = {
            "a": self._model_float("sensor.huis_afkoel_constante"),
            "b": self._model_float("sensor.huis_solar_gain_constante"),
            "c": self._model_float(self.WIND_LOSS_FACTOR_ENTITY),
            "g_fast": self._model_float(
                self.THERMAL_MODEL_GAIN_ENTITY, "g_snel"
            ),
            "g_slow": self._model_float(
                self.THERMAL_MODEL_GAIN_ENTITY, "g_traag"
            ),
            "g_outdoor": self._model_float(
                self.THERMAL_MODEL_GAIN_ENTITY, "tout_helling_per_10k"
            ),
            "tau_hours": self._model_float(
                self.THERMAL_MODEL_GAIN_ENTITY, "tau_traag_uur"
            ),
            "power_kw": self._model_float(
                self.THERMAL_MODEL_GAIN_ENTITY, "typisch_vermogen_kw"
            ),
            "internal_night": self._model_float(
                self.THERMAL_MODEL_INTERNAL_GAIN_ENTITY, "nacht"
            ),
            "internal_day_extra": self._model_float(
                self.THERMAL_MODEL_INTERNAL_GAIN_ENTITY, "dag_extra_07_23"
            ),
        }
        missing = [key for key, value in model.items() if value is None]
        if missing:
            return None, f"modelparameters ontbreken: {', '.join(missing)}"
        if (
            model["a"] <= 0
            or model["b"] < 0
            or model["c"] < 0
            or model["g_fast"] < 0
            or model["g_slow"] < 0
            or model["g_fast"] + model["g_slow"] <= 0
            or model["tau_hours"] <= 0
            or model["power_kw"] <= 0
        ):
            return None, "modelparameters liggen buiten het bruikbare bereik"
        model["quality"] = quality
        model["source"] = self.get_state(
            self.THERMAL_MODEL_GAIN_ENTITY, attribute="bron"
        )
        model["input"] = self.get_state(
            self.THERMAL_MODEL_GAIN_ENTITY, attribute="invoer"
        )
        return model, None

    def _simulate_thermal_model_step(
        self, model, indoor_temp, ema_power, outdoor_temp, wind_speed,
        pv_kw, power_kw, step_time
    ):
        step_hours = self.SIMULATION_QUARTER_MINUTES / 60.0
        alpha = 1.0 - math.exp(-step_hours / model["tau_hours"])
        next_ema = ema_power + alpha * (power_kw - ema_power)
        day = float(
            self.MODEL_DAY_START_HOUR <= step_time.hour < self.MODEL_DAY_END_HOUR
        )
        temperature_rate = (
            (model["a"] + model["c"] * wind_speed) * (outdoor_temp - indoor_temp)
            + model["b"] * pv_kw
            + model["g_fast"] * power_kw
            + model["g_slow"] * next_ema
            + model["g_outdoor"] * next_ema * (outdoor_temp - 7.0) / 10.0
            + model["internal_night"]
            + model["internal_day_extra"] * day
        )
        return indoor_temp + temperature_rate * step_hours, next_ema

    def _simulate_model_heating_windows(
        self, model, forecast, now, indoor_temp, outdoor_temp, wind_speed,
        target_temp, average_price, heating_is_on
    ):
        horizon = self.SIMULATION_HORIZON_QUARTERS
        power_kw = model["power_kw"]
        initial_ema = self._estimate_initial_ema_power(
            model, now, heating_is_on
        )
        passive_states = [(indoor_temp, initial_ema)]
        minimum_temp, minimum_q = indoor_temp, 0
        need_q = 0 if indoor_temp < target_temp - 0.4 else None

        def pv_at(quarter):
            target_time = now + datetime.timedelta(
                minutes=quarter * self.SIMULATION_QUARTER_MINUTES
            )
            return self.get_forecast_value(
                forecast, target_time, "pv_kw", self.SIMULATION_QUARTER_MINUTES
            ) or 0.0

        for quarter in range(horizon):
            target_time = now + datetime.timedelta(
                minutes=quarter * self.SIMULATION_QUARTER_MINUTES
            )
            temp, ema = passive_states[-1]
            temp, ema = self._simulate_thermal_model_step(
                model, temp, ema, outdoor_temp, wind_speed,
                pv_at(quarter), 0.0, target_time
            )
            passive_states.append((temp, ema))
            if temp < minimum_temp:
                minimum_temp, minimum_q = temp, quarter + 1
            if need_q is None and temp < target_temp - 0.4:
                need_q = quarter + 1

        candidates = []
        for start_q, (start_temp, start_ema) in enumerate(passive_states):
            temp, ema = start_temp, start_ema
            duration = 0
            while (
                (temp < target_temp or duration == 0)
                and duration < self.MAX_MODEL_HEATING_QUARTERS
            ):
                quarter = start_q + duration
                target_time = now + datetime.timedelta(
                    minutes=quarter * self.SIMULATION_QUARTER_MINUTES
                )
                temp, ema = self._simulate_thermal_model_step(
                    model, temp, ema, outdoor_temp, wind_speed,
                    pv_at(quarter), power_kw, target_time
                )
                duration += 1
            if temp < target_temp:
                continue
            prices = [
                self._forecast_price(
                    forecast,
                    now + datetime.timedelta(
                        minutes=(start_q + offset)
                        * self.SIMULATION_QUARTER_MINUTES
                    ),
                    average_price,
                )
                for offset in range(duration)
            ]
            candidates.append({
                "start_q": start_q,
                "duration": duration,
                "average_price": sum(prices) / duration,
            })

        if not candidates:
            return None
        current_window = next(
            (candidate for candidate in candidates if candidate["start_q"] == 0),
            None,
        )
        best_window = min(candidates, key=lambda candidate: candidate["average_price"])
        if current_window is None:
            current_window = candidates[0]
        return {
            "need_q": need_q,
            "minimum_temp": minimum_temp,
            "minimum_q": minimum_q,
            "current_duration": current_window["duration"],
            "current_average_price": current_window["average_price"],
            "best_start_q": best_window["start_q"],
            "best_average_price": best_window["average_price"],
        }

    def _estimate_initial_ema_power(self, model, now, heating_is_on):
        expected_state = "on" if heating_is_on else "off"
        if self.get_state(self.vrijgave_helper) != expected_state:
            return 0.0
        last_switch = self.get_state(
            self.vrijgave_helper, attribute="last_switch_at"
        )
        try:
            changed_at = self.parse_datetime(last_switch)
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=datetime.timezone.utc)
            elapsed_hours = max(0.0, (now - changed_at).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            return 0.0
        fraction = math.exp(-elapsed_hours / model["tau_hours"])
        return model["power_kw"] * (1.0 - fraction if heating_is_on else fraction)

    def _forecast_price(self, forecast, target_dt, average_price):
        price = self.get_forecast_value(
            forecast, target_dt, "net_prijs", self.SIMULATION_QUARTER_MINUTES
        )
        if price is not None:
            return price

        preceding_prices = [
            (item["tijd"], item["net_prijs"])
            for item in forecast
            if item["tijd"] <= target_dt
        ]
        if preceding_prices:
            return max(preceding_prices, key=lambda item: item[0])[1]
        return average_price

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
        k_afkoel = self._get_float_state(
            "sensor.huis_afkoel_constante", self.FALLBACK_K_AFKOEL
        )
        k_solar = self._get_float_state(
            "sensor.huis_solar_gain_constante", self.FALLBACK_K_SOLAR
        )
        k_wind = self._get_float_state(
            self.WIND_LOSS_FACTOR_ENTITY, self.FALLBACK_K_WIND
        )
        k_opwarm = self._get_float_state(
            "sensor.huis_opwarm_constante", self.FALLBACK_K_OPWARM
        )
        wind_speed = self._get_float_state(self.WIND_SPEED_ENTITY, 0.0)
        if k_wind < 0:
            self.log(
                f"Sensor {self.WIND_LOSS_FACTOR_ENTITY} mag niet negatief zijn; "
                "windcorrectie uitgeschakeld.",
                level="WARNING",
            )
            k_wind = 0.0
        if wind_speed < 0:
            self.log(
                f"Sensor {self.WIND_SPEED_ENTITY} mag niet negatief zijn; "
                "windcorrectie uitgeschakeld.",
                level="WARNING",
            )
            wind_speed = 0.0
        if k_opwarm <= 0:
            self.log(
                "Sensor sensor.huis_opwarm_constante moet groter zijn dan 0; "
                f"fallback {self.FALLBACK_K_OPWARM} gebruikt.",
                level="WARNING",
            )
            k_opwarm = self.FALLBACK_K_OPWARM

        # Actuele sensoren en stooklijn
        t_binnen = self._get_float_state("sensor.woonkamer_temperatuur_gemiddeld", 19.0)
        zon_nu = self._get_float_state("sensor.solaredge_i1_ac_power", 0.0)
        t_buiten = self._get_float_state("sensor.kantoor_buiten_temperatuur", 10.0)
        stooklijn_setpoint = int(self._get_float_state("sensor.warmtepomp_stooklijn", 20.0))

        # Controleer de werkelijke fysieke status van de Hitachi via jouw sensor
        wp_modus = self.get_state(self.thermo_entity)
        target_temp = self._get_float_state(
            self.thermo_entity, 19.5, attribute="temperature"
        )
        t_target_min = target_temp - 0.4
        now = self.datetime(aware=True)
        diagnostics = {
            "indoor_temperature_c": t_binnen,
            "outdoor_temperature_c": t_buiten,
            "target_temperature_c": target_temp,
            "target_minimum_temperature_c": t_target_min,
            "current_pv_power_w": zon_nu,
            "wind_speed_kmh": wind_speed,
            "warmtepomp_model_status": self.get_state("sensor.huis_model_kwaliteit"),
            "warmtepomp_model_updated_at": self.get_state(
                "sensor.huis_model_kwaliteit", attribute="laatste_run"
            ),
            "warmtepomp_model_source": self.get_state(
                self.THERMAL_MODEL_GAIN_ENTITY, attribute="bron"
            ),
        }
        thermal_model, model_fallback_reason = self._load_thermal_model()
        diagnostics["thermal_model_simulation"] = (
            "geleerd_rc" if thermal_model is not None else "eenvoudig_fallback"
        )
        diagnostics["thermal_model_fallback_reason"] = model_fallback_reason
        if thermal_model is not None:
            diagnostics["thermal_model_source"] = thermal_model["source"]
            diagnostics["thermal_model_input"] = thermal_model["input"]
        if wp_modus == "off":
            self.log("STOP: De thermostaat staat handmatig UIT. EMS doet niets.")
            self._publish_diagnostic(
                "handmatig_uit", "thermostaat staat handmatig uit", diagnostics
            )
            return

        # --- HARD LIMITS (Voorkom dat het te warm wordt) ---
        if t_binnen >= (target_temp + 1.0) or t_binnen >= 21.5:
            self.log(f"HARD CRITIEK UIT: Huis is te warm ({t_binnen}°C). Limiet van {target_temp + 1.0}°C bereikt.")
            self._decide_and_control(
                "uit", "harde boventemperatuurgrens bereikt", False, 20, diagnostics
            )
            return

        # --- COMFORT OVERRIDES (Te koud) ---
        if t_binnen < 18.0 or (target_temp - t_binnen > 0.8):
            self.log(f"FORCE AAN: Comfort-override activeert. T_binnen ({t_binnen}) is te laag.")
            self._decide_and_control(
                "aan",
                "comfortoverride: binnentemperatuur te laag",
                True,
                stooklijn_setpoint,
                diagnostics,
            )
            return

        # Forecastmatrix levert zowel de PV-verwachting als het all-in tarief.
        forecast_state = self.get_state(self.forecast_entity)
        forecast_status = self.get_state(self.forecast_entity, attribute="status")
        forecast_generated_at = self.get_state(
            self.forecast_entity, attribute="generated_at"
        )
        raw_forecast = self.get_state(self.forecast_entity, attribute="forecast")
        forecast = self._validate_forecast(
            raw_forecast
        )
        diagnostics.update({
            "forecast_state": forecast_state,
            "forecast_status": forecast_status,
            "forecast_generated_at": forecast_generated_at,
            "forecast_valid_rows": len(forecast),
            "forecast_total_rows": (
                len(raw_forecast) if isinstance(raw_forecast, list) else None
            ),
            "input_quality": (
                "forecast_onbeschikbaar"
                if forecast_state in (None, "unknown", "unavailable")
                else "forecast_geldig"
                if forecast
                else "forecastmatrix_ongeldig"
            ),
        })
        if (
            forecast_state in (None, "unknown", "unavailable")
            or not forecast
        ):
            self.log(
                f"DATA ERROR: {self.forecast_entity} is niet beschikbaar "
                "of bevat geen forecastmatrix. "
                "Fallback op basisregeling.",
                level="WARNING",
            )
            self._decide_and_control(
                "basisregeling",
                "forecast ontbreekt; fallback op binnentemperatuur",
                t_binnen < t_target_min,
                stooklijn_setpoint,
                diagnostics,
            )
            return

        nu = now
        huidige_prijs = self.get_forecast_value(forecast, nu, "net_prijs", 15)
        if huidige_prijs is None:
            self.log(
                f"DATA ERROR: Geen bruikbaar huidig tarief in {self.forecast_entity}. "
                "Fallback op basisregeling.",
                level="WARNING",
            )
            self._decide_and_control(
                "basisregeling",
                "huidig forecasttarief ontbreekt; fallback op binnentemperatuur",
                t_binnen < t_target_min,
                stooklijn_setpoint,
                diagnostics,
            )
            return
        gemiddelde_forecast_prijs = sum(
            row["net_prijs"] for row in forecast
        ) / len(forecast)
        diagnostics.update({
            "current_net_price_eur_kwh": huidige_prijs,
            "average_forecast_price_eur_kwh": gemiddelde_forecast_prijs,
        })

        # 3. Bereken benodigde stooktijd
        graden_te_overbruggen = max(0.1, target_temp - t_binnen)
        kwartieren_stooktijd = int(
            math.ceil((graden_te_overbruggen / k_opwarm) * (60 / self.SIMULATION_QUARTER_MINUTES))
        )
        kwartieren_stooktijd = max(1, kwartieren_stooktijd)

        # 4. De Thermische & Financiële Simulatie (4 uur vooruitkijken)
        model_result = None
        if thermal_model is not None:
            model_result = self._simulate_model_heating_windows(
                thermal_model,
                forecast,
                nu,
                t_binnen,
                t_buiten,
                wind_speed,
                target_temp,
                gemiddelde_forecast_prijs,
                self.get_state(self.wp_status_sensor) == self.wp_status_aan_waarde,
            )
            if model_result is None:
                self.log(
                    "Thermisch model kon geen haalbaar verwarmingsvenster simuleren; "
                    "eenvoudige simulatie wordt gebruikt.",
                    level="WARNING",
                )
                diagnostics["thermal_model_simulation"] = "eenvoudig_fallback"
                diagnostics["thermal_model_fallback_reason"] = (
                    "model bereikte de doeltemperatuur niet binnen de simulatiegrens"
                )

        if model_result is not None:
            stoken_nodig_vanaf_q = model_result["need_q"]
            kwartieren_stooktijd = model_result["current_duration"]
            laagste_gem_prijs = model_result["best_average_price"]
            moment_start_stoken = model_result["best_start_q"]
            laagste_voorspelde_temperatuur = model_result["minimum_temp"]
            laagste_temperatuur_bij_q = model_result["minimum_q"]
            gemiddelde_prijs_huidig_venster = model_result["current_average_price"]
        else:
            sim_t = t_binnen
            stoken_nodig_vanaf_q = None
            laagste_gem_prijs = 999.0
            moment_start_stoken = 0
            laagste_voorspelde_temperatuur = t_binnen
            laagste_temperatuur_bij_q = 0
            horizon_kwartieren = self.SIMULATION_HORIZON_QUARTERS

            for q in range(0, horizon_kwartieren + 1):
                doel_tijd = nu + datetime.timedelta(
                    minutes=q * self.SIMULATION_QUARTER_MINUTES
                )
                zon_q = (
                    self.get_forecast_value(
                        forecast, doel_tijd, "pv_kw", self.SIMULATION_QUARTER_MINUTES
                    )
                    or 0.0
                )
                sim_t = self._simulate_temperature_step(
                    sim_t, t_buiten, zon_q, k_afkoel, k_solar, k_wind, wind_speed
                )
                if sim_t < laagste_voorspelde_temperatuur:
                    laagste_voorspelde_temperatuur = sim_t
                    laagste_temperatuur_bij_q = q
                if sim_t < t_target_min and stoken_nodig_vanaf_q is None:
                    stoken_nodig_vanaf_q = q

                totaal_prijs_sessie = 0
                for s in range(q, q + kwartieren_stooktijd):
                    tijd_s = nu + datetime.timedelta(
                        minutes=s * self.SIMULATION_QUARTER_MINUTES
                    )
                    totaal_prijs_sessie += self._forecast_price(
                        forecast, tijd_s, gemiddelde_forecast_prijs
                    )
                gem_prijs_sessie = totaal_prijs_sessie / kwartieren_stooktijd
                if gem_prijs_sessie < laagste_gem_prijs:
                    laagste_gem_prijs = gem_prijs_sessie
                    moment_start_stoken = q
            gemiddelde_prijs_huidig_venster = None

        diagnostics.update({
            "predicted_minimum_temperature_c": round(
                laagste_voorspelde_temperatuur, 3
            ),
            "predicted_minimum_at": (
                nu + datetime.timedelta(
                    minutes=laagste_temperatuur_bij_q
                    * self.SIMULATION_QUARTER_MINUTES
                )
            ).isoformat(),
            "heating_need_in_minutes": (
                stoken_nodig_vanaf_q * self.SIMULATION_QUARTER_MINUTES
                if stoken_nodig_vanaf_q is not None
                else None
            ),
            "best_heating_window_start_minutes": (
                moment_start_stoken * self.SIMULATION_QUARTER_MINUTES
            ),
            "best_window_average_price_eur_kwh": round(laagste_gem_prijs, 4)
            if laagste_gem_prijs < 999.0
            else None,
        })

        # 5. De Beslissing voor NU
        prijs_nu_all_in = huidige_prijs

        # STRATEGIE A: Er is een warmtebehoefte voorspeld binnen 4 uur
        if stoken_nodig_vanaf_q is not None:
            if gemiddelde_prijs_huidig_venster is not None:
                gem_prijs_nu = gemiddelde_prijs_huidig_venster
            else:
                totaal_prijs_nu = 0
                for s in range(0, kwartieren_stooktijd):
                    tijd_s = nu + datetime.timedelta(
                        minutes=s * self.SIMULATION_QUARTER_MINUTES
                    )
                    totaal_prijs_nu += self._forecast_price(
                        forecast, tijd_s, gemiddelde_forecast_prijs
                    )
                gem_prijs_nu = totaal_prijs_nu / kwartieren_stooktijd

            verwarming_staat_aan = (
                self.get_state(self.vrijgave_helper) == "on"
            )
            prijsruimte = self.PRICE_WINDOW_TOLERANCE_EUR
            if verwarming_staat_aan:
                prijsruimte += self.PRICE_HYSTERESIS_EUR
            if gem_prijs_nu <= laagste_gem_prijs + prijsruimte:
                self.log(f"AAN: Noodzaak over {stoken_nodig_vanaf_q*15}m. Prijs nu ({round(gem_prijs_nu,3)}) is optimaal.")
                diagnostics["current_heating_window_average_price_eur_kwh"] = round(
                    gem_prijs_nu, 4
                )
                self._decide_and_control(
                    "aan",
                    "warmtevraag voorspeld en prijs nu binnen de optimale marge",
                    True,
                    stooklijn_setpoint,
                    diagnostics,
                )
            else:
                self.log(f"WACHTEN: Prijs nu ({round(gem_prijs_nu,3)}) > straks ({round(laagste_gem_prijs,3)}) bij t+{moment_start_stoken*15}m.")
                diagnostics["current_heating_window_average_price_eur_kwh"] = round(
                    gem_prijs_nu, 4
                )
                self._decide_and_control(
                    "wachten",
                    "later verwarmingsvenster is goedkoper",
                    False,
                    20,
                    diagnostics,
                )

        # STRATEGIE B: OPPORTUNISTISCH VOORVERWARMEN (Bufferen)
        else:
            verwarming_staat_aan = self.get_state(self.vrijgave_helper) == "on"
            prijsgrens = self.BUFFER_PRICE_THRESHOLD_EUR
            if verwarming_staat_aan:
                prijsgrens += self.PRICE_HYSTERESIS_EUR
            if (
                (prijs_nu_all_in < prijsgrens or zon_nu > self.PV_BUFFER_POWER_W)
                and t_binnen < (target_temp + self.BUFFER_TEMPERATURE_MARGIN_C)
            ):
                self.log(f"AAN (Bufferen): Prijs is laag ({round(prijs_nu_all_in,3)}) of zon schijnt. Vloer laden. T_binnen: {t_binnen}°C")
                self._decide_and_control(
                    "aan_bufferen",
                    "gunstige prijs of actueel PV-vermogen; ruimte voor bufferwarmte",
                    True,
                    stooklijn_setpoint,
                    diagnostics,
                )
            else:
                # FIX: Log level verlaagd naar DEBUG om overstroming van je reguliere logbestanden te voorkomen
                self.log(f"IDLE: Geen noodzaak en geen gunstige buffercondities. T_binnen: {round(t_binnen,2)}°C.", level="DEBUG")
                self._decide_and_control(
                    "uit",
                    "geen voorspelde warmtevraag of gunstige bufferconditie",
                    False,
                    20,
                    diagnostics,
                )

    def stuur_warmtepomp(self, state_wensen, setpoint_wensen) -> bool:
        nu = self.datetime(aware=True)
        current_state_raw = self.get_state(self.wp_status_sensor)
        if current_state_raw in (None, "unknown", "unavailable"):
            self.log(
                f"Geen betrouwbare status van {self.wp_status_sensor}; "
                "Modbus-aansturing overgeslagen.",
                level="ERROR",
            )
            return False
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
        except Exception as err:
            self.error(f"Modbus-aansturing van de warmtepomp is mislukt: {err}")
            return False

        if self.get_state(self.vrijgave_helper) != helper_state:
            self._save_switch_tracking(new_state_bool, nu, setpoint)
        else:
            self.set_state(
                self.vrijgave_helper,
                attributes={"geregistreerd_setpoint": setpoint},
            )

        self.run_in(
            self._verify_warmtepomp_status,
            self.STATUS_VERIFY_INTERVAL_SECONDS,
            expected_on=new_state_bool,
            attempt=1,
        )
        return True

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
        attempt = kwargs["attempt"]
        current_status = self.get_state(self.wp_status_sensor)
        if current_status in (None, "unknown", "unavailable"):
            self.log(
                f"Modbus-statuscontrole {attempt}/{self.STATUS_VERIFY_MAX_ATTEMPTS} "
                f"nog niet mogelijk: {self.wp_status_sensor} is {current_status!r}.",
                level="WARNING",
            )
            actual_on = None
        else:
            actual_on = current_status == self.wp_status_aan_waarde

        if actual_on == expected_on:
            return
        if attempt < self.STATUS_VERIFY_MAX_ATTEMPTS:
            self.log(
                f"Modbus-statuscontrole {attempt}/{self.STATUS_VERIFY_MAX_ATTEMPTS}: "
                f"verwacht {'AAN' if expected_on else 'UIT'}, "
                f"gemeten {current_status!r}; controle wordt herhaald.",
                level="WARNING",
            )
            self.run_in(
                self._verify_warmtepomp_status,
                self.STATUS_VERIFY_INTERVAL_SECONDS,
                expected_on=expected_on,
                attempt=attempt + 1,
            )
            return

        self.log(
            "Modbus-statuscontrole is na "
            f"{self.STATUS_VERIFY_MAX_ATTEMPTS * self.STATUS_VERIFY_INTERVAL_SECONDS}s "
            f"nog niet geslaagd: verwacht {'AAN' if expected_on else 'UIT'}, "
            f"gemeten {current_status!r}.",
            level="ERROR",
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
