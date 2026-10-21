"""EMS Verwarming (AppDaemon) - v2

Rolverdeling
------------
* Generic thermostat (climate.warmtepomp_regeling): doeltemperatuur, hvac-modus en comfortvraag.
  Zijn actuator is input_boolean.verwarming_vrijgave ("comfort vraagt verwarming").
* Dit script: beslist elk kwartier (en direct bij een wijziging van de thermostaat) of
  switch.warmtepomp_status_dummy aan of uit moet. Dat is de enige schakelaar die jouw bestaande
  automatisering "Warmtepomp stooklijn schrijven" gebruikt voor Modbus (registers 1002/1005).
  Dit script schrijft dus zelf geen Modbus meer.
* woningthermischeanalyse.py: leert het huis (a, b, c, g, interne winst) en de COP(Tout)-relatie.

Beslisvolgorde
--------------
 1. EMS uitgeschakeld of fout        -> dummy volgt de thermostaat (vrijgave)
 2. thermostaat hvac_mode = off       -> dummy uit
 3. te warm (harde grens)             -> dummy uit
 4. comfortvraag (vrijgave aan) of temperatuur onder doel - cold_tolerance -> dummy aan
 5. anders: simulatie per kwartier met voorspelde buitentemperatuur, wind, PV, prijs en COP.
    - Elk kandidaat-startmoment moet de temperatuur boven doel - cold_tolerance houden.
    - Kies het goedkoopste kandidaat (totale kosten = prijs x elektrisch vermogen).
    - Geen voorspelde warmtevraag: opportunistisch bufferen bij lage prijs of veel PV.
"""

import datetime
import math
import threading
import time
import traceback

import appdaemon.plugins.hass.hassapi as hass


class EMSVerwarming(hass.Hass):

    # ------------------------------------------------------------------ standaardwaarden
    MINIMALE_WISSELTIJD_SECONDEN = 1200
    QUARTER_S = 900
    QUARTER_H = 0.25
    SIMULATION_QUARTER_MINUTES = 15
    SIMULATION_HORIZON_QUARTERS = 16
    MAX_MODEL_HEATING_QUARTERS = 96
    MODEL_DAY_START_HOUR = 7
    MODEL_DAY_END_HOUR = 23
    REF_TOUT = 7.0

    FALLBACK_K_AFKOEL = 0.015
    FALLBACK_K_SOLAR = 0.14
    FALLBACK_K_OPWARM = 0.45        # K/h bruto, alleen voor het eenvoudige noodmodel
    FALLBACK_DEFAULT_COP = 3.0

    BUFFER_PRICE_THRESHOLD_EUR = 0.14
    PRICE_WINDOW_TOLERANCE_EUR = 0.005
    PRICE_HYSTERESIS_EUR = 0.005
    PV_BUFFER_POWER_W = 1500
    BUFFER_TEMPERATURE_MARGIN_C = 0.5
    MIN_START_DEFICIT_C = 0.2       # EMS start pas als de temperatuur minstens zoveel onder het doel zit
    OVERSHOOT_HARD_C = 1.0
    MAX_INDOOR_C = 21.5

    FORECAST_VALUE_KEYS = ("pv_kw", "net_prijs")
    WP_STATUS_AAN = "operation_state_heat_thermo_on"
    OUTDOOR_RANGE = (-30.0, 45.0)
    WIND_UNIT_TO_KMH = {"km/h": 1.0, "m/s": 3.6, "mph": 1.609344, "kn": 1.852, "ft/s": 1.09728}
    MODEL_MAX_AGE_DAYS = 7
    COP_ENTITY = "sensor.huis_wp_cop"
    DIAGNOSTIC_ENTITY = "sensor.ems_verwarming_diagnostiek"
    THERMAL_MODEL_QUALITY_ENTITY = "sensor.huis_model_kwaliteit"
    THERMAL_MODEL_GAIN_ENTITY = "sensor.huis_verwarming_gain"
    THERMAL_MODEL_INTERNAL_GAIN_ENTITY = "sensor.huis_interne_winst"

    # ------------------------------------------------------------------ opstarten
    def initialize(self):
        self.log("--- EMS Verwarming v2 gestart (schakelt de dummy; thermostaat levert de vrijgave) ---")
        self._lock = threading.Lock()
        self._weather_cache = None
        self._last_outdoor = None
        self._last_command_ts = None
        self._warned = set()
        self._laad_config()

        self.listen_state(self._trigger, self.vrijgave)
        self.listen_state(self._trigger, self.climate)
        self.listen_state(self._trigger, self.climate, attribute="temperature")

        # Draai op de kwartiergrenzen (EPEX-kwartieren), 5 s na het hele kwartier
        nu = self.datetime()
        volgende = nu.replace(minute=(nu.minute // 15) * 15, second=5, microsecond=0) \
            + datetime.timedelta(minutes=15)
        self.run_every(self._cyclus, volgende, self.QUARTER_S)
        self.run_in(self._cyclus, 20)

    def _arg(self, naam, default, cast=float):
        waarde = self.args.get(naam, default)
        try:
            return cast(waarde)
        except (TypeError, ValueError):
            self.log(f"Ongeldige waarde voor {naam}: {waarde!r}; standaard {default!r}.", level="WARNING")
            return cast(default)

    def _laad_config(self):
        a = self.args
        self.climate = a.get("climate", "climate.warmtepomp_regeling")
        self.vrijgave = a.get("vrijgave_entity", "input_boolean.verwarming_vrijgave")
        self.dummy = a.get("dummy_switch", "switch.warmtepomp_status_dummy")
        self.ems_actief_entity = a.get("ems_actief_entity", "input_boolean.ems_actief")
        self.cold_tol = max(0.0, self._arg("cold_tolerance", 0.4))      # gelijk aan generic thermostat
        self.hot_tol = max(0.0, self._arg("hot_tolerance", 0.0))
        self.indoor_entity = a.get("binnen_sensor", "sensor.woonkamer_temperatuur_gemiddeld")
        buiten = a.get("buiten_sensoren") or [
            "sensor.woonkamer_buiten2_temperatuur", "sensor.kantoor_buiten_temperatuur",
            "sensor.sonoff_tuin_temperature"]
        self.outdoor_entities = [buiten] if isinstance(buiten, str) else list(buiten)
        self.outdoor_min = 2 if len(self.outdoor_entities) >= 2 else 1
        self.pv_entity = a.get("pv_sensor", "sensor.solaredge_i1_ac_power")
        self.wind_entity = a.get("wind_sensor", "sensor.thuis_windsnelheid")
        self.wp_status_sensor = a.get("wp_status_sensor", "sensor.control_unit_operation_state_2")
        self.thermal_power_entity = a.get("thermisch_vermogen_sensor", "sensor.control_unit_thermal_power_heating")
        self.electric_power_entities = a.get("elektrisch_vermogen_sensoren", [
            "sensor.shelly_warmtepomp_binnenunit_active_power", "sensor.shelly_warmtepomp_buitenunit_active_power"])
        self.forecast_entity = a.get("forecast_entity", "sensor.energie_core_forecast")
        self.weather_entities = list(a.get("weather_entities", ["weather.knmi_thuis", "weather.beerweer"]))
        self.weather_cache_min = self._arg("weather_cache_minuten", 30, float)
        self.weather_bias_tau_h = max(0.5, self._arg("weather_bias_tau_uur", 3.0))
        self.horizon = max(4, self._arg("horizon_kwartieren", self.SIMULATION_HORIZON_QUARTERS, int))
        self.start_deficit = max(0.0, self._arg("min_start_deficit", self.MIN_START_DEFICIT_C))
        self.max_indoor = self._arg("max_binnentemperatuur", self.MAX_INDOOR_C)
        self.overshoot_hard = self._arg("max_overschrijding", self.OVERSHOOT_HARD_C)
        self.min_switch_s = self._arg("minimale_wisseltijd_seconden", self.MINIMALE_WISSELTIJD_SECONDEN, float)
        self.buffer_price = self._arg("buffer_prijsgrens", self.BUFFER_PRICE_THRESHOLD_EUR)
        self.pv_buffer_w = self._arg("pv_buffer_vermogen_w", self.PV_BUFFER_POWER_W)
        self.buffer_margin = self._arg("buffer_marge", self.BUFFER_TEMPERATURE_MARGIN_C)
        self.default_cop = self._arg("standaard_cop", self.FALLBACK_DEFAULT_COP)

    # ------------------------------------------------------------------ hulpfuncties
    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            self.log(message, level="WARNING")

    def _float(self, entity, attribute=None):
        value = self.get_state(entity, attribute=attribute)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def _parse_dt(self, value):
        if isinstance(value, datetime.datetime):
            dt = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(text)
            except ValueError:
                dt = self.parse_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt

    def _publish_diagnostic(self, decision, reason, attributes=None):
        attrs = {
            "friendly_name": "EMS verwarming diagnostiek", "icon": "mdi:heat-pump",
            "decision": decision, "reason": reason, "updated_at": self.datetime(aware=True).isoformat(),
        }
        if attributes:
            attrs.update(attributes)
        try:
            self.set_state(self.DIAGNOSTIC_ENTITY, state=decision, attributes=attrs)
        except Exception as err:  # noqa: BLE001
            self.log(f"Publiceren van {self.DIAGNOSTIC_ENTITY} mislukt: {err}", level="ERROR")

    # ------------------------------------------------------------------ triggers
    def _trigger(self, *_args, **_kwargs):
        self.run_in(self._cyclus, 2)

    def _cyclus(self, _kwargs=None):
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._beslis()
        except Exception as err:  # noqa: BLE001
            self.log(f"Fout in EMS-cyclus: {err!r}\n{traceback.format_exc()}", level="ERROR")
            try:
                self._volg_thermostaat("fout")
                self._publish_diagnostic("fout", f"uitzondering: {err!r}; dummy volgt de thermostaat")
            except Exception as err2:  # noqa: BLE001
                self.log(f"Ook de terugval mislukte: {err2!r}", level="ERROR")
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ schakelaar
    def _dummy_state(self):
        state = self.get_state(self.dummy)
        return state if state in ("on", "off") else None

    def _seconds_in_state(self, now):
        """Seconden sinds de dummy voor het laatst veranderde (HA last_changed of eigen commando)."""
        candidates = []
        try:
            last = self.get_state(self.dummy, attribute="last_changed")
            if last:
                candidates.append(self._parse_dt(last))
        except (TypeError, ValueError):
            pass
        if self._last_command_ts is not None:
            candidates.append(self._last_command_ts)
        if not candidates:
            return 0.0
        return max(0.0, (now - max(candidates)).total_seconds())

    def _zet_dummy(self, wanted_on, now):
        current = self._dummy_state()
        if current is None:
            self.log(f"{self.dummy} heeft geen geldige status; schakelen overgeslagen.", level="ERROR")
            return False
        if (current == "on") == wanted_on:
            return True
        try:
            self.call_service("switch/turn_on" if wanted_on else "switch/turn_off", entity_id=self.dummy)
        except Exception as err:  # noqa: BLE001
            self.log(f"Schakelen van {self.dummy} mislukt: {err!r}", level="ERROR")
            return False
        self._last_command_ts = now
        self.log(f"SCHAKEL: {self.dummy} -> {'AAN' if wanted_on else 'UIT'}")
        self.run_in(self._verify_dummy, 10, expected="on" if wanted_on else "off")
        return True

    def _verify_dummy(self, kwargs):
        if self._dummy_state() != kwargs["expected"]:
            self.log(f"{self.dummy} staat na 10 s niet op {kwargs['expected']}; controleer thermostaat/automatiseringen "
                     f"die deze schakelaar ook aansturen.", level="WARNING")

    def _volg_thermostaat(self, reden):
        """Terugval: dummy = vrijgave (gedrag zoals zonder EMS). Geen wisseltijdbeveiliging."""
        vrijgave = self.get_state(self.vrijgave)
        if vrijgave not in ("on", "off"):
            return False
        return self._zet_dummy(vrijgave == "on", self.datetime(aware=True))

    def _apply(self, now, wanted_on, decision, reason, diagnostics, exempt_min_on=False, exempt_min_off=False):
        current = self._dummy_state()
        seconds = self._seconds_in_state(now)
        final = wanted_on
        if current == "on" and not wanted_on and not exempt_min_on and seconds < self.min_switch_s:
            self.log(f"PENDEL-REMEDIATIE: uitschakelen uitgesteld, minimale looptijd niet bereikt "
                     f"({int(seconds / 60)}m).")
            final, decision, reason = True, "aan", f"minimale looptijd ({reason})"
        elif current == "off" and wanted_on and not exempt_min_off and seconds < self.min_switch_s:
            self.log(f"PENDEL-REMEDIATIE: inschakelen uitgesteld, minimale rusttijd niet bereikt "
                     f"({int(seconds / 60)}m).")
            final, decision, reason = False, "wachten", f"minimale rusttijd ({reason})"
        ok = self._zet_dummy(final, now)
        diagnostics["dummy_command_ok"] = ok
        diagnostics["dummy_seconds_in_state"] = int(seconds)
        self._publish_diagnostic(decision, reason, diagnostics)

    # ------------------------------------------------------------------ forecast (prijs / PV)
    def _validate_forecast(self, forecast):
        if not isinstance(forecast, list):
            return []
        rows = []
        for row in forecast:
            if not isinstance(row, dict) or not row.get("tijd"):
                continue
            try:
                start = self._parse_dt(row["tijd"])
                values = {k: float(row[k]) for k in self.FORECAST_VALUE_KEYS}
                if not all(math.isfinite(v) for v in values.values()):
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            rows.append({"tijd": start, **values})
        return rows

    def _kwartier_reeksen(self, forecast, nu, n, average_price):
        """Prijs en PV per kwartier vanaf nu (index 0..n-1); ontbrekende prijs = laatst bekende."""
        by_q = {int(r["tijd"].timestamp() // self.QUARTER_S): r for r in forecast}
        q0 = int(nu.timestamp() // self.QUARTER_S)
        known = [(k, r["net_prijs"]) for k, r in by_q.items() if k <= q0]
        last_price = max(known)[1] if known else average_price
        prices, pv = [], []
        for i in range(n):
            row = by_q.get(q0 + i)
            if row is not None:
                last_price = row["net_prijs"]
            prices.append(last_price)
            pv.append(row["pv_kw"] if row is not None else 0.0)
        return prices, pv

    # ------------------------------------------------------------------ buitentemperatuur / wind
    def _outdoor_now(self):
        waarden = []
        for ent in self.outdoor_entities:
            v = self._float(ent)
            if v is not None and self.OUTDOOR_RANGE[0] <= v <= self.OUTDOOR_RANGE[1]:
                waarden.append(v)
        if len(waarden) >= self.outdoor_min:
            waarden.sort()
            m = len(waarden) // 2
            mediaan = waarden[m] if len(waarden) % 2 else 0.5 * (waarden[m - 1] + waarden[m])
            self._last_outdoor = (self.datetime(aware=True), mediaan)
            return mediaan
        return None

    def _weer_rijen(self, entity):
        """Uurvoorspelling (ts, temperatuur C, wind km/h) van een weather-entiteit."""
        res = self.call_service("weather/get_forecasts", entity_id=entity, type="hourly",
                                hass_timeout=30, timeout=40, suppress_log_messages=True)
        items = self._zoek_forecast(res, entity)
        if not items:
            attr = self.get_state(entity, attribute="forecast")  # oudere HA-versies
            items = attr if isinstance(attr, list) else []
        t_unit = self.get_state(entity, attribute="temperature_unit") or "°C"
        w_unit = self.get_state(entity, attribute="wind_speed_unit")
        factor = self.WIND_UNIT_TO_KMH.get(str(w_unit).strip().lower() if w_unit else "km/h", 1.0)
        if w_unit and str(w_unit).strip().lower() not in self.WIND_UNIT_TO_KMH:
            self._warn_once(f"wu_{entity}", f"{entity}: onbekende windeenheid {w_unit!r}; km/h aangenomen.")
        rijen = []
        for it in items:
            try:
                ts = self._parse_dt(it["datetime"]).timestamp()
                temp = float(it["temperature"])
                if str(t_unit).strip() in ("°F", "F"):
                    temp = (temp - 32.0) * 5.0 / 9.0
                wind = float(it.get("wind_speed", 0.0) or 0.0) * factor
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(temp) and self.OUTDOOR_RANGE[0] <= temp <= self.OUTDOOR_RANGE[1]:
                rijen.append((ts, temp, max(0.0, wind)))
        rijen.sort()
        return rijen

    def _zoek_forecast(self, obj, entity, diepte=0):
        """Zoekt {entity: {'forecast': [...]}} in een (geneste) serviceresponse."""
        if diepte > 5:
            return None
        if isinstance(obj, dict):
            if entity in obj:
                v = obj[entity]
                if isinstance(v, dict) and isinstance(v.get("forecast"), list):
                    return v["forecast"]
                if isinstance(v, list):
                    return v
            for v in obj.values():
                r = self._zoek_forecast(v, entity, diepte + 1)
                if r:
                    return r
        return None

    def _combineer_weer(self, bronnen):
        """Gemiddelde van de bronnen; uren met één bron worden gecorrigeerd met het gemiddelde verschil."""
        if not bronnen:
            return None
        per_bron = [{ts: (t, w) for ts, t, w in rijen} for _, rijen in bronnen]
        alle = sorted(set().union(*[set(d) for d in per_bron]))
        gemeenschappelijk = [ts for ts in alle if all(ts in d for d in per_bron)]
        offsets = []
        for d in per_bron:
            if len(per_bron) > 1 and len(gemeenschappelijk) >= 3:
                gem_t = [sum(b[ts][0] for b in per_bron) / len(per_bron) for ts in gemeenschappelijk]
                gem_w = [sum(b[ts][1] for b in per_bron) / len(per_bron) for ts in gemeenschappelijk]
                offsets.append((sum(g - d[ts][0] for g, ts in zip(gem_t, gemeenschappelijk)) / len(gem_t),
                                sum(g - d[ts][1] for g, ts in zip(gem_w, gemeenschappelijk)) / len(gem_w)))
            else:
                offsets.append((0.0, 0.0))
        uit = []
        for ts in alle:
            aanwezig = [(d[ts], offsets[i]) for i, d in enumerate(per_bron) if ts in d]
            if len(aanwezig) == len(per_bron):
                uit.append((ts, sum(a[0][0] for a in aanwezig) / len(aanwezig),
                            sum(a[0][1] for a in aanwezig) / len(aanwezig)))
            else:  # minder bronnen dan normaal: corrigeer voor structureel verschil
                uit.append((ts, sum(a[0][0] + a[1][0] for a in aanwezig) / len(aanwezig),
                            max(0.0, sum(a[0][1] + a[1][1] for a in aanwezig) / len(aanwezig))))
        return uit

    def _weather_series(self, now):
        cache = self._weather_cache
        if cache and (now - cache["at"]).total_seconds() < self.weather_cache_min * 60:
            return cache["data"], cache["bronnen"]
        bronnen = []
        for ent in self.weather_entities:
            try:
                rijen = self._weer_rijen(ent)
            except Exception as err:  # noqa: BLE001
                self.log(f"Weersvoorspelling {ent} mislukt: {err!r}", level="WARNING")
                continue
            if rijen:
                bronnen.append((ent, rijen))
        data = self._combineer_weer(bronnen)
        namen = [b[0] for b in bronnen]
        self._weather_cache = {"at": now, "data": data, "bronnen": namen}
        return data, namen

    @staticmethod
    def _interp(series, ts):
        if ts <= series[0][0]:
            return series[0][1], series[0][2]
        if ts >= series[-1][0]:
            return series[-1][1], series[-1][2]
        lo, hi = 0, len(series) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if series[mid][0] <= ts:
                lo = mid
            else:
                hi = mid
        t0, a0, w0 = series[lo]
        t1, a1, w1 = series[hi]
        f = (ts - t0) / (t1 - t0)
        return a0 + f * (a1 - a0), w0 + f * (w1 - w0)

    def _weather_per_quarter(self, nu, n, t_now, wind_now):
        """Buitentemperatuur en wind per kwartier: voorspelling, bijgestuurd met de gemeten waarde."""
        series, bronnen = self._weather_series(nu)
        if not series:
            return [t_now] * n, [wind_now] * n, []
        f_t, f_w = self._interp(series, nu.timestamp())
        offset = max(-5.0, min(5.0, t_now - f_t))
        temps, winds = [], []
        for i in range(n):
            ts = nu.timestamp() + i * self.QUARTER_S
            t, w = self._interp(series, ts)
            temps.append(t + offset * math.exp(-(i * self.QUARTER_H) / self.weather_bias_tau_h))
            winds.append(w)
        return temps, winds, bronnen

    # ------------------------------------------------------------------ thermisch model en COP
    def _load_thermal_model(self, now):
        quality = self.get_state(self.THERMAL_MODEL_QUALITY_ENTITY)
        accepted = self.get_state(self.THERMAL_MODEL_GAIN_ENTITY, attribute="laatste_geaccepteerd")
        if quality != "ok":
            try:
                age_days = (now - self._parse_dt(accepted)).total_seconds() / 86400.0
            except (TypeError, ValueError, AttributeError):
                return None, f"modelkwaliteit is {quality!r} en er is geen recente geaccepteerde fit"
            if age_days > self.MODEL_MAX_AGE_DAYS:
                return None, f"laatste geaccepteerde fit is {age_days:.1f} dagen oud"
        g = self.THERMAL_MODEL_GAIN_ENTITY
        model = {
            "a": self._float("sensor.huis_afkoel_constante"),
            "b": self._float("sensor.huis_solar_gain_constante"),
            "c": self._float("sensor.huis_wind_loss_factor"),
            "g_fast": self._float(g, "g_snel"), "g_slow": self._float(g, "g_traag"),
            "g_outdoor": self._float(g, "tout_helling_per_10k"), "tau_hours": self._float(g, "tau_traag_uur"),
            "power_kw": self._float(g, "typisch_vermogen_kw"),
            "internal_night": self._float(self.THERMAL_MODEL_INTERNAL_GAIN_ENTITY, "nacht"),
            "internal_day_extra": self._float(self.THERMAL_MODEL_INTERNAL_GAIN_ENTITY, "dag_extra_07_23"),
        }
        missing = [k for k, v in model.items() if v is None]
        if missing:
            return None, f"modelparameters ontbreken: {', '.join(missing)}"
        tau_huis = 1.0 / model["a"] if model["a"] > 0 else None
        if (tau_huis is None or not (20.0 <= tau_huis <= 300.0) or model["b"] < 0 or model["c"] < 0
                or model["g_fast"] < 0 or model["g_slow"] < 0 or model["g_fast"] + model["g_slow"] <= 0
                or model["g_fast"] + model["g_slow"] > 3.0 or model["tau_hours"] <= 0 or model["power_kw"] <= 0):
            return None, "modelparameters liggen buiten het bruikbare bereik"
        model["input"] = self.get_state(g, attribute="invoer") or "thermisch"
        model["source"] = self.get_state(g, attribute="bron")
        model["simulation"] = "geleerd_rc"
        return model, None

    def _fallback_model(self):
        """Eenvoudig noodmodel: oude constanten + vaste opwarmsnelheid; geen vloerinertie."""
        k_opwarm = self._float("sensor.huis_opwarm_constante")
        if k_opwarm is None or k_opwarm <= 0:
            k_opwarm = self.FALLBACK_K_OPWARM
        return {
            "a": self._float("sensor.huis_afkoel_constante") or self.FALLBACK_K_AFKOEL,
            "b": self._float("sensor.huis_solar_gain_constante") or self.FALLBACK_K_SOLAR,
            "c": max(0.0, self._float("sensor.huis_wind_loss_factor") or 0.0),
            "g_fast": k_opwarm, "g_slow": 0.0, "g_outdoor": 0.0, "tau_hours": 1.0, "power_kw": 1.0,
            "internal_night": 0.0, "internal_day_extra": 0.0,
            "input": "thermisch", "source": "fallback", "simulation": "eenvoudig_fallback",
        }

    def _load_cop(self):
        """COP(Tout) en vermogens uit sensor.huis_wp_cop; None als (nog) niet beschikbaar."""
        cop_ref = self._float(self.COP_ENTITY)
        if cop_ref is None or not (1.5 <= cop_ref <= 7.0):
            return None
        e = self.COP_ENTITY
        return {
            "cop_ref": cop_ref, "cop_slope": self._float(e, "cop_helling_per_10k"),
            "p_el_ref": self._float(e, "p_el_ref_kw"), "p_el_slope": self._float(e, "p_el_helling_per_10k_kw"),
            "p_th_typ": self._float(e, "p_th_typ_kw"),
        }

    def _heat_params(self, model, cop, tout):
        """(invoervermogen voor het model, elektrisch vermogen, COP) bij een buitentemperatuur."""
        if cop is None:
            p_th = model["power_kw"]
            return (p_th, p_th / self.default_cop, self.default_cop)
        x = (tout - self.REF_TOUT) / 10.0
        c = cop["cop_ref"] + (cop["cop_slope"] * x if cop["cop_slope"] is not None else 0.0)
        c = max(1.0, min(8.0, c))
        if cop["p_el_ref"] is not None:
            p_el = cop["p_el_ref"] + (cop["p_el_slope"] * x if cop["p_el_slope"] is not None else 0.0)
        else:
            p_el = (cop["p_th_typ"] or model["power_kw"]) / c
        p_el = max(0.3, min(8.0, p_el))
        p_th = p_el * c
        return (p_th if model["input"] != "elektrisch" else p_el, p_el, c)

    def _history_steps(self, entity, start, end):
        kwargs = {"entity_id": entity, "start_time": start, "end_time": end}
        try:
            res = self.get_history(timeout=20, **kwargs)
        except TypeError:
            res = self.get_history(**kwargs)
        items = res[0] if isinstance(res, list) and res and isinstance(res[0], list) else res
        punten = []
        for it in items or []:
            try:
                punten.append((self._parse_dt(it.get("last_changed") or it.get("last_updated")).timestamp(),
                               float(it["state"])))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return sorted(punten)

    def _initial_ema(self, model, now):
        """Trage EMA van het verwarmingsvermogen uit de laatste uren (zelfde definitie als de analyse)."""
        tau = model["tau_hours"]
        window_h = min(8.0, max(3.0 * tau, 1.0))
        start = now - datetime.timedelta(hours=window_h)
        entities = [self.thermal_power_entity] if model["input"] != "elektrisch" else list(self.electric_power_entities)
        try:
            reeksen = [self._history_steps(e, start, now) for e in entities]
        except Exception as err:  # noqa: BLE001
            self._warn_once("hist", f"Vermogenshistorie niet beschikbaar ({err!r}); EMA start op 0.")
            return 0.0
        alle = sorted({ts for r in reeksen for ts, _ in r})
        if not alle:
            return 0.0
        schaal = 1.0
        waarden = [v for r in reeksen for _, v in r]
        if waarden and sorted(waarden)[int(0.95 * (len(waarden) - 1))] > 30.0:
            schaal = 0.001   # Watt -> kW
        ema, laatste = 0.0, start.timestamp()
        huidig = [0.0] * len(reeksen)
        idx = [0] * len(reeksen)
        for ts in alle + [now.timestamp()]:
            p = sum(huidig) * schaal
            dt_h = max(0.0, (ts - laatste) / 3600.0)
            ema = p + (ema - p) * math.exp(-dt_h / tau)
            laatste = ts
            for k, r in enumerate(reeksen):
                while idx[k] < len(r) and r[idx[k]][0] <= ts:
                    huidig[k] = r[idx[k]][1]
                    idx[k] += 1
        return max(0.0, ema)

    def _simulate_step(self, model, indoor, ema, outdoor, wind, pv_kw, power_kw, step_time):
        alpha = 1.0 - math.exp(-self.QUARTER_H / model["tau_hours"])
        next_ema = ema + alpha * (power_kw - ema)
        day = float(self.MODEL_DAY_START_HOUR <= step_time.hour < self.MODEL_DAY_END_HOUR)
        rate = ((model["a"] + model["c"] * wind) * (outdoor - indoor) + model["b"] * pv_kw
                + model["g_fast"] * power_kw + model["g_slow"] * next_ema
                + model["g_outdoor"] * next_ema * (outdoor - self.REF_TOUT) / 10.0
                + model["internal_night"] + model["internal_day_extra"] * day)
        return indoor + rate * self.QUARTER_H, next_ema

    # ------------------------------------------------------------------ vensters plannen
    def _plan(self, model, cop, nu, indoor, temps, winds, pv, prices, target, floor, goal, heating_on, ema0):
        """Passieve baan + kandidaat-startmomenten met comforteis en totale kosten."""
        horizon = self.horizon
        n = len(temps)
        times = [nu + datetime.timedelta(minutes=15 * q) for q in range(n)]
        heat = [self._heat_params(model, cop, temps[q]) for q in range(n)]

        passive = [(indoor, ema0)]
        min_pref = [indoor]
        need_q = None
        for q in range(horizon):
            t, e = self._simulate_step(model, passive[-1][0], passive[-1][1], temps[q], winds[q], pv[q], 0.0, times[q])
            passive.append((t, e))
            min_pref.append(min(min_pref[-1], t))
            if need_q is None and t < floor:
                need_q = q + 1

        candidates = []
        for s in range(horizon + 1):
            t_s, e_s = passive[s]
            continuing = (s == 0 and heating_on and t_s < goal)
            if t_s > goal - self.start_deficit and not continuing:
                continue
            temp, ema, dur, tmin = t_s, e_s, 0, t_s
            cost = energy = 0.0
            while (temp < goal or dur == 0) and dur < self.MAX_MODEL_HEATING_QUARTERS and s + dur < n:
                q = s + dur
                p_in, p_el, _ = heat[q]
                temp, ema = self._simulate_step(model, temp, ema, temps[q], winds[q], pv[q], p_in, times[q])
                tmin = min(tmin, temp)
                cost += prices[q] * p_el * self.QUARTER_H
                energy += p_el * self.QUARTER_H
                dur += 1
            if temp < goal:
                continue
            if min(min_pref[s], tmin) < floor:
                continue
            candidates.append({"start_q": s, "duration": dur, "cost": cost, "energy": energy,
                               "avg_price": cost / energy if energy > 0 else prices[s],
                               "min_temp": min(min_pref[s], tmin)})
        return {"passive": passive, "need_q": need_q, "candidates": candidates,
                "minimum_temp": min(p[0] for p in passive), "heat": heat}

    # ------------------------------------------------------------------ hoofdbeslissing
    def _beslis(self):
        now = self.datetime(aware=True)
        nu = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        diag = {}

        # 1. EMS uitgeschakeld?
        if self.ems_actief_entity and self.get_state(self.ems_actief_entity) == "off":
            self._volg_thermostaat("ems_uit")
            self._publish_diagnostic("ems_uit", "input_boolean.ems_actief staat uit; dummy volgt de thermostaat", diag)
            return

        # 2. Basisgegevens
        t_binnen = self._float(self.indoor_entity)
        climate_state = self.get_state(self.climate)
        target = self._float(self.climate, "temperature")
        vrijgave = self.get_state(self.vrijgave)
        dummy_state = self._dummy_state()
        if t_binnen is None or target is None or climate_state in (None, "unavailable", "unknown"):
            self._warn_once("basis", "Binnentemperatuur of thermostaat niet beschikbaar; EMS wijzigt niets "
                                     "(dummy volgt de thermostaat).")
            self._volg_thermostaat("geen_data")
            self._publish_diagnostic("geen_data", "binnentemperatuur of thermostaat niet beschikbaar", diag)
            return
        if vrijgave not in ("on", "off"):
            self._warn_once("vrijgave", f"{self.vrijgave} heeft geen geldige status. Stel de actuator van de "
                                        f"thermostaat in op deze boolean; nu geldt alleen de temperatuurgrens.")
        floor = target - self.cold_tol
        goal = target + self.hot_tol
        diag.update({
            "indoor_temperature_c": t_binnen, "target_temperature_c": target,
            "comfort_floor_c": round(floor, 2), "goal_temperature_c": round(goal, 2),
            "cold_tolerance": self.cold_tol, "hot_tolerance": self.hot_tol,
            "thermostat_demand": vrijgave, "dummy_state": dummy_state,
            "wp_status": self.get_state(self.wp_status_sensor),
        })

        # 3. Thermostaat uit
        if climate_state == "off":
            self._apply(now, False, "handmatig_uit", "thermostaat staat uit", diag,
                        exempt_min_on=True)
            return

        # 4. Harde bovengrens
        if t_binnen >= target + self.overshoot_hard or t_binnen >= self.max_indoor:
            self.log(f"HARD UIT: {t_binnen} C bereikt de bovengrens.")
            self._apply(now, False, "uit", "harde boventemperatuurgrens bereikt", diag, exempt_min_on=True)
            return

        # 5. Comfortvraag: thermostaat vraagt verwarming of we zitten onder de ondergrens
        if vrijgave == "on" or t_binnen < floor:
            reden = ("thermostaat vraagt verwarming (comfort)" if vrijgave == "on"
                     else "binnentemperatuur onder de comfortondergrens")
            self._apply(now, True, "aan", reden, diag, exempt_min_off=True)
            return

        # 6. Prijs/PV-forecast
        raw_forecast = self.get_state(self.forecast_entity, attribute="forecast")
        forecast = self._validate_forecast(raw_forecast)
        diag.update({"forecast_valid_rows": len(forecast),
                     "forecast_status": self.get_state(self.forecast_entity, attribute="status"),
                     "forecast_generated_at": self.get_state(self.forecast_entity, attribute="generated_at")})
        zon_nu = self._float(self.pv_entity) or 0.0
        if not forecast:
            self._warn_once("forecast", f"{self.forecast_entity} bevat geen bruikbare forecast; geen prijsoptimalisatie.")
            self._apply(now, False, "basisregeling", "forecast ontbreekt; alleen thermostaat bepaalt", diag)
            return
        gem_prijs = sum(r["net_prijs"] for r in forecast) / len(forecast)
        n = self.horizon + self.MAX_MODEL_HEATING_QUARTERS + 1
        prices, pv = self._kwartier_reeksen(forecast, nu, n, gem_prijs)
        prijs_nu = prices[0]
        diag["current_net_price_eur_kwh"] = prijs_nu

        # 7. Weer, model, COP
        t_buiten = self._outdoor_now()
        if t_buiten is None and self._last_outdoor and (now - self._last_outdoor[0]).total_seconds() < 3 * 3600:
            t_buiten = self._last_outdoor[1]
        wind_nu = self._float(self.wind_entity)
        temps, winds, bronnen = self._weather_per_quarter(
            nu, n, t_buiten if t_buiten is not None else 10.0, wind_nu if wind_nu is not None else 0.0)
        if t_buiten is None:
            if bronnen:
                t_buiten = temps[0]
            self._warn_once("buiten", "Geen bruikbare buitentemperatuur; voorspelling/aanname gebruikt.")
        model, reden_fb = self._load_thermal_model(now)
        if model is None:
            model = self._fallback_model()
        cop = self._load_cop()
        diag.update({
            "outdoor_temperature_c": t_buiten, "weather_sources": bronnen,
            "outdoor_forecast_next_4h_c": [round(temps[i], 1) for i in (0, 4, 8, 12, 16) if i < n],
            "wind_forecast_next_4h_kmh": [round(winds[i], 1) for i in (0, 4, 8, 12, 16) if i < n],
            "thermal_model_simulation": model["simulation"], "thermal_model_fallback_reason": reden_fb,
            "thermal_model_source": model["source"], "thermal_model_input": model["input"],
            "cop_model": "geleerd" if cop else f"aanname_{self.default_cop}",
            "current_pv_power_w": zon_nu,
        })

        heating_on = dummy_state == "on"
        ema0 = self._initial_ema(model, now) if model["simulation"] == "geleerd_rc" else 0.0
        diag["initial_ema_kw"] = round(ema0, 3)
        plan = self._plan(model, cop, nu, t_binnen, temps, winds, pv, prices, target, floor, goal, heating_on, ema0)
        candidates = plan["candidates"]
        need_q = plan["need_q"]
        diag.update({
            "predicted_minimum_temperature_c": round(plan["minimum_temp"], 3),
            "heating_need_in_minutes": need_q * 15 if need_q is not None else None,
            "valid_windows": len(candidates),
        })

        # 8a. Voorspelde warmtevraag binnen de horizon
        if need_q is not None:
            if not candidates:
                self._apply(now, True, "aan", "geen startmoment houdt de comfortondergrens; nu starten", diag)
                return
            best = min(candidates, key=lambda c: (c["cost"], c["start_q"]))
            current = next((c for c in candidates if c["start_q"] == 0), None)
            diag.update({"best_window_start_minutes": best["start_q"] * 15,
                         "best_window_cost_eur": round(best["cost"], 4),
                         "best_window_avg_price_eur_kwh": round(best["avg_price"], 4)})
            if current is None:
                self._apply(now, False, "wachten",
                            f"verwarmen start liefst over {best['start_q'] * 15} min (goedkoopste venster)", diag)
                return
            tol = self.PRICE_WINDOW_TOLERANCE_EUR + (self.PRICE_HYSTERESIS_EUR if heating_on else 0.0)
            diag["current_window_cost_eur"] = round(current["cost"], 4)
            if current["cost"] <= best["cost"] + tol * current["energy"]:
                self._apply(now, True, "aan", "warmtevraag voorspeld en kosten nu binnen de optimale marge", diag)
            else:
                self._apply(now, False, "wachten", "later verwarmingsvenster is goedkoper", diag)
            return

        # 8b. Geen voorspelde warmtevraag: opportunistisch bufferen
        grens = self.buffer_price + (self.PRICE_HYSTERESIS_EUR if heating_on else 0.0)
        if (prijs_nu < grens or zon_nu > self.pv_buffer_w) and t_binnen < target + self.buffer_margin:
            self._apply(now, True, "aan_bufferen", "gunstige prijs of actueel PV-vermogen; ruimte voor bufferwarmte", diag)
        else:
            self._apply(now, False, "uit", "geen voorspelde warmtevraag of gunstige bufferconditie", diag)