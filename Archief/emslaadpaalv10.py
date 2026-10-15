import hassapi as hass
import datetime
import math
import threading
import json
from typing import List, Dict, Optional, Any, Tuple
from dateutil import tz
from dateutil.parser import parse

class PeblarHorizonPlanner(hass.Hass):
    # ===== CONFIGURATIE =====
    MAX_PAAL_KW = 11.0
    ACCU_CAP_KWH = 77.0
    MIN_SOC_SAFETY = 10.0
    VEILIGHEIDSMARGE_PERCENT = 15.0
    FALLBACK_KM_PER_PERCENT = 4.0
    FALLBACK_VERBRUIK_KW = 0.11
    SECONDS_PER_QUARTER = 900
    TOLERANCE = 0.001
    FALLBACK_EPEX_PRIJS = 0.20
    LAADRENDEMENT = 0.90
    QUARTER_HOURS = 0.25
    MIN_VERBRUIK_KW = 0.04
    AMPERE_TOLERANCE = 0.1
    PLAN_VALIDITY_MINUTES = 30
    SERVICE_CALL_DELAY = 5
    EPEX_UNIT_MULTIPLIER = 1.0
    MAX_GROEP_KW = 25.0
    MIN_AMPERE = 6.0
    MAX_AMPERE = 16.0
    DEBUG_LOGGING = False
    MAX_SOC_JUMP_PER_QUARTER = 10.0
    CRITICAL_PRICE_THRESHOLD = 0.00
    MIN_RUN_MINUTES = 30
    MIN_LAADVERMOGEN_ZON_KW = 1.38

    # NIEUWE CONFIGURATIE VOOR BELASTINGEN (2026)
    ENERGIEBELASTING_PER_KWH = 0.15
    LEVERANCIERSKOSTEN_PER_KWH = 0.03
    BTW_PERCENTAGE = 21.0
    MAX_EPEX_FORECAST_HOURS = 48

    # Persistentie instellingen
    PERSISTENCE_ENTITY = "input_text.peblar_horizon_planner_persistent_data"
    PERSISTENCE_ENABLED = True
    GRAPH_DATA_ENTITY = "sensor.peblar_horizon_planner_graph_data"
    ANNULEER_SERVICE = "peblar_horizon_planner/annuleer_planning"

    # ===== ENTITY CONFIGURATIE =====
    DEFAULT_ENTITIES = {
        "soc_entity": "sensor.fordpass_wf0spbef7ssj42833_soc",
        "bereik_entity": "sensor.fordpass_wf0spbef7ssj42833_elveh",
        "peblar_energie_entity": "sensor.peblar_ev_charger_levenslange_energie",
        "vertrektijd_entity": "input_datetime.ford_capri_ev_charging_ready_setpoint",
        "gewenste_km_entity": "input_number.ford_capri_ev_gewenste_kilometers",
        "solcast_today_entity": "sensor.solcast_pv_forecast_forecast_today",
        "solcast_tomorrow_entity": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "epex_prijs_entity": "sensor.nordpool_huidige_kwartierprijs_nl",
        "peblar_switch_entity": "switch.peblar_ev_charger_opladen",
        "peblar_mode_entity": "select.peblar_ev_charger_slim_laden",
        "peblar_laadlimiet_entity": "number.peblar_ev_charger_laadlimiet",
        "verbruik_huis_entity": "sensor.verbruik_huis",
        "peblar_status_entity": "sensor.peblar_ev_charger_status",
        "peblar_power_entity": "sensor.peblar_ev_charger_laadvermogen",
        "push_notification_entity": "notify.mobile_app_iphone",
    }

    FASEN = 3
    SPANNING_V = 230.0
    LOCAL_TZ_NAME = "Europe/Amsterdam"

    def initialize(self):
        self.log("Peblar Horizon Planner v10 opgestart.")
        self._lock = threading.RLock()
        self._load_configuration()
        self._local_tz = tz.gettz(self.LOCAL_TZ_NAME)
        self.MIN_LAADVERMOGEN_KW = self._ampere_naar_kw(self.MIN_AMPERE)

        # Plan state
        self.plan = {}
        self.matrix_kwartieren = []
        self.db_cache = {}
        self.db_cache_loaded = False
        self.prijs_dict = {}
        self.forecast_dict = {}
        self.last_plan_calculation = None
        self.plan_valid_until = None
        self.doel_soc = None
        self.sessie_doel_kwh = 0.0
        self.cumulatief_geladen_kwh = 0.0
        self.initiele_energie = None
        self.last_power_on_time = None
        self.last_power_off_time = None
        self.sessie_actief = False
        self.vorige_soc = None
        self.vorige_laadvermogen = None
        self.last_soc_update = None
        self.last_mode_call = None
        self.last_switch_call = None
        self.last_laadlimiet_call = None
        self._desired_mode = "Pure solar"
        self._desired_power_on = False
        self._desired_kw = 0.0
        self._control_generation = 0
        self._pending_control_timer = None
        self._debounce_timer = None
        self.planning_geannuleerd = False

        # Laad persistente data
        self._load_persistent_data()
        self._register_services()
        self._schedule_kwartier_timer()

        # Triggers
        self.listen_state(self._herplan_debounced, self.vertrektijd_entity)
        self.listen_state(self._herplan_debounced, self.soc_entity)
        self.listen_state(self._herplan_debounced, self.gewenste_km_entity)
        self.listen_state(self._herplan_debounced, self.solcast_today_entity)
        self.listen_state(self._herplan_debounced, self.solcast_tomorrow_entity)
        self.listen_state(self._auto_status_changed, self.peblar_status_entity)
        self.listen_state(self._herplan_debounced, self.peblar_energie_entity)
        self.listen_state(self._herplan_debounced, self.peblar_switch_entity)
        self.listen_state(self._herplan_debounced, self.peblar_mode_entity)
        self.listen_state(self._herplan_debounced, self.peblar_laadlimiet_entity)
        self.listen_state(self._laadvermogen_changed, self.peblar_power_entity)
        self.listen_state(self._epex_prijzen_updated, self.epex_prijs_entity)

        self._laad_verbruikshistorie()
        self.run_in(self._startup_plan, 5)

    def _register_services(self):
        self.listen_service(self._annuleer_planning_service, self.ANNULEER_SERVICE)

    def _annuleer_planning_service(self, **kwargs):
        with self._lock:
            self.log("Planning geannuleerd via service call.")
            self.planning_geannuleerd = True
            self._clear_plan()
            self._set_desired_state("Pure solar", False, 0.0)
            self._reset_laadtracking()
            self._save_persistent_data()
            self._send_push_notification("Planning Geannuleerd", "De laadplanning is geannuleerd.")

    def _load_configuration(self):
        for key, default in self.DEFAULT_ENTITIES.items():
            setattr(self, key, self.args.get(key, default))
        self.FASEN = int(self.args.get("fasen", self.FASEN))
        self.SPANNING_V = float(self.args.get("spanning_v", self.SPANNING_V))
        self.MIN_VERBRUIK_KW = float(self.args.get("min_verbruik_kw", self.MIN_VERBRUIK_KW))
        self.PLAN_VALIDITY_MINUTES = int(self.args.get("plan_validity_minutes", self.PLAN_VALIDITY_MINUTES))
        self.SERVICE_CALL_DELAY = int(self.args.get("service_call_delay", self.SERVICE_CALL_DELAY))
        self.MAX_GROEP_KW = float(self.args.get("max_groep_kw", self.MAX_GROEP_KW))
        self.MIN_AMPERE = float(self.args.get("min_ampere", self.MIN_AMPERE))
        self.MAX_AMPERE = float(self.args.get("max_ampere", self.MAX_AMPERE))
        self.DEBUG_LOGGING = bool(self.args.get("debug_logging", self.DEBUG_LOGGING))
        self.EPEX_UNIT_MULTIPLIER = float(self.args.get("epex_unit_multiplier", self.EPEX_UNIT_MULTIPLIER))
        self.LAADRENDEMENT = float(self.args.get("laadrendement", self.LAADRENDEMENT))
        self.MAX_SOC_JUMP_PER_QUARTER = float(self.args.get("max_soc_jump", self.MAX_SOC_JUMP_PER_QUARTER))
        self.CRITICAL_PRICE_THRESHOLD = float(self.args.get("critical_price_threshold", self.CRITICAL_PRICE_THRESHOLD))
        self.MIN_RUN_MINUTES = int(self.args.get("min_run_minutes", self.MIN_RUN_MINUTES))
        self.MIN_LAADVERMOGEN_ZON_KW = float(self.args.get("min_laadvermogen_zon_kw", self.MIN_LAADVERMOGEN_ZON_KW))
        self.ENERGIEBELASTING_PER_KWH = float(self.args.get("energiebelasting_per_kwh", self.ENERGIEBELASTING_PER_KWH))
        self.LEVERANCIERSKOSTEN_PER_KWH = float(self.args.get("leverancierskosten_per_kwh", self.LEVERANCIERSKOSTEN_PER_KWH))
        self.BTW_PERCENTAGE = float(self.args.get("btw_percentage", self.BTW_PERCENTAGE))
        self.MAX_EPEX_FORECAST_HOURS = int(self.args.get("max_epex_forecast_hours", self.MAX_EPEX_FORECAST_HOURS))
        self.PERSISTENCE_ENTITY = self.args.get("persistence_entity", self.PERSISTENCE_ENTITY)
        self.PERSISTENCE_ENABLED = bool(self.args.get("persistence_enabled", self.PERSISTENCE_ENABLED))
        self.GRAPH_DATA_ENTITY = self.args.get("graph_data_entity", self.GRAPH_DATA_ENTITY)

    def _load_persistent_data(self):
        if not self.PERSISTENCE_ENABLED:
            return
        try:
            state = self.get_state(self.PERSISTENCE_ENTITY)
            if state and state.lower() not in ["unavailable", "unknown"]:
                data = json.loads(state)
                with self._lock:
                    self.plan = {datetime.datetime.fromisoformat(k): v for k, v in data.get("plan", {}).items()}
                    self.doel_soc = data.get("doel_soc")
                    self.sessie_doel_kwh = data.get("sessie_doel_kwh", 0.0)
                    self.cumulatief_geladen_kwh = data.get("cumulatief_geladen_kwh", 0.0)
                    self.initiele_energie = data.get("initiele_energie")
                    self.planning_geannuleerd = data.get("planning_geannuleerd", False)
        except Exception as e:
            self.warning(f"Kon persistente data niet laden: {self._sanitize_error(e)}")

    def _save_persistent_data(self):
        if not self.PERSISTENCE_ENABLED:
            return
        try:
            data = {
                "plan": {str(k): v for k, v in self.plan.items()},
                "doel_soc": self.doel_soc,
                "sessie_doel_kwh": self.sessie_doel_kwh,
                "cumulatief_geladen_kwh": self.cumulatief_geladen_kwh,
                "initiele_energie": self.initiele_energie,
                "planning_geannuleerd": self.planning_geannuleerd,
            }
            self.set_state(self.PERSISTENCE_ENTITY, state=json.dumps(data))
        except Exception as e:
            self.error(f"Kon persistente data niet opslaan: {self._sanitize_error(e)}")

    def _epex_prijzen_updated(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            self._build_prijs_dict()
            if self.plan and self.last_plan_calculation:
                self.log("EPEX prijzen geupdate, herbereken planning...")
                self.bereken_laadplan()

    def bereken_totaal_tarief(self, epex_prijs: float) -> float:
        totaal = epex_prijs + self.ENERGIEBELASTING_PER_KWH + self.LEVERANCIERSKOSTEN_PER_KWH
        btw_factor = 1 + (self.BTW_PERCENTAGE / 100)
        return round(totaal * btw_factor, 4)

    def _startup_plan(self, kwargs):
        with self._lock:
            if not self.planning_geannuleerd:
                self.bereken_laadplan()

    def _clear_plan(self):
        with self._lock:
            self.plan = {}
            self.matrix_kwartieren = []
            self.plan_valid_until = None
            self.doel_soc = None
            self.sessie_doel_kwh = 0.0

    def _reset_laadtracking(self):
        with self._lock:
            self.cumulatief_geladen_kwh = 0.0
            self.initiele_energie = None
            self.sessie_doel_kwh = 0.0
            self.sessie_actief = False
            self.vorige_soc = None
            self.last_power_on_time = None
            self.last_power_off_time = None

    def _send_push_notification(self, title: str, message: str):
        if hasattr(self, 'push_notification_entity') and self.push_notification_entity:
            try:
                service = self.push_notification_entity.replace("notify.", "notify/")
                self.call_service(service, title=title, message=message, data={"priority": "high", "ttl": 0})
            except Exception as e:
                self.error(f"Fout bij verzenden pushmelding: {self._sanitize_error(e)}")

    def _check_kritieke_sensoren(self) -> bool:
        kritieke_sensoren = [self.soc_entity, self.bereik_entity, self.peblar_energie_entity,
                             self.peblar_status_entity, self.peblar_switch_entity, self.peblar_mode_entity]
        for sensor in kritieke_sensoren:
            state = self.get_state(sensor)
            if state is None or str(state).lower() in ["unavailable", "unknown"]:
                self.error(f"Kritieke sensor onbeschikbaar: {sensor}")
                self._send_push_notification("Peblar Fout", f"Kritieke sensor onbeschikbaar: {sensor}")
                return False
        return True

    def _laadvermogen_changed(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if new is None:
                return
            try:
                current_power = float(new)
                status = self.get_state(self.peblar_status_entity)
                if status and status.lower() in ["suspendedev", "finished"] and current_power <= self.TOLERANCE:
                    self.log("Auto gestopt door max SoC. Sessie afsluiten.")
                    self._send_push_notification("Laadsessie Voltooid", "Auto heeft maximaal SoC bereikt.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
            except (ValueError, TypeError):
                pass

    def _schedule_kwartier_timer(self):
        nu = datetime.datetime.now(tz.UTC)
        quarter = (nu.minute // 15) * 15
        last_quarter = nu.replace(minute=quarter, second=0, microsecond=0, tzinfo=tz.UTC)
        next_time = last_quarter + datetime.timedelta(minutes=15, seconds=1)
        self.run_at(self._kwartier_callback, next_time)

    def _kwartier_callback(self, kwargs):
        with self._lock:
            self.voer_schakeling_uit()
            self._update_graph_data()
        self._schedule_kwartier_timer()

    def _herplan_debounced(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            if self._debounce_timer is not None:
                self.cancel_timer(self._debounce_timer)
            self._debounce_timer = self.run_in(self._herplan_execute, 0.5, trigger_entity=entity)

    def _herplan_execute(self, kwargs):
        self._debounce_timer = None
        trigger_entity = kwargs.get("trigger_entity", "onbekend")
        with self._lock:
            if self.planning_geannuleerd:
                return
            self.log(f"Herplanning getriggerd door {trigger_entity}.")
            if self.sessie_actief:
                self.bereken_laadplan()

    def _auto_status_changed(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            if new is None or str(new).lower() in ["unavailable", "unknown", "available"]:
                self.log(f"Auto losgekoppeld (status: {new}). Harde stop.")
                self._send_push_notification("Auto Losgekoppeld", f"Auto is losgekoppeld. Status: {new}")
                self._set_desired_state("Pure solar", False, 0.0)
                self._reset_laadtracking()
                self._save_persistent_data()
            elif str(new).lower() in ["preparing", "charging", "suspendedev", "suspendedevse", "finishing"]:
                huidige_soc = self.get_sensor_float(self.soc_entity)
                if not self.sessie_actief:
                    self.sessie_actief = True
                    self.initiele_energie = None
                    self.vorige_soc = huidige_soc
                    self.planning_geannuleerd = False
                    self.log(f"Auto verbonden (status: {new}). Nieuwe sessie gestart, SoC={huidige_soc}%")
                self._herplan_debounced(entity, attribute, old, new, kwargs)

    def _get_fallback_prijs(self, kwartier_start: datetime.datetime) -> float:
        return self.FALLBACK_EPEX_PRIJS

    def _sanitize_error(self, error: Exception) -> str:
        error_str = str(error)
        for word in ["password", "pwd", "host", "192.168", "10.0", "172.", "secret", "token"]:
            error_str = error_str.replace(word, "[REDACTED]")
        return error_str

    def _check_service_call_delay(self, last_call: Optional[datetime.datetime]) -> bool:
        if last_call is None:
            return True
        elapsed = (datetime.datetime.now(tz.UTC) - last_call).total_seconds()
        return elapsed >= self.SERVICE_CALL_DELAY

    def _remaining_delay(self, last_call: Optional[datetime.datetime]) -> float:
        if last_call is None:
            return 0.0
        elapsed = (datetime.datetime.now(tz.UTC) - last_call).total_seconds()
        return max(0.0, self.SERVICE_CALL_DELAY - elapsed)

    def _extract_timestamp(self, state_dict: Any) -> Optional[datetime.datetime]:
        if not isinstance(state_dict, dict):
            try:
                state_dict = dict(state_dict)
            except (TypeError, ValueError):
                return None
        for key in ["last_changed", "last_reported", "last_updated", "last_seen"]:
            if key in state_dict:
                try:
                    ts_str = str(state_dict[key])
                    dt = parse(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz.UTC)
                    else:
                        dt = dt.astimezone(tz.UTC)
                    return dt
                except (ValueError, TypeError, OverflowError):
                    continue
        return None

    def get_sensor_float(self, entity_id: str, default: Optional[float] = None) -> Optional[float]:
        state = self.get_state(entity_id)
        if state is None or str(state).lower() in ["unavailable", "unknown"]:
            return default
        try:
            return max(0.0, float(state))
        except (ValueError, TypeError):
            return default

    def _is_auto_verbonden(self) -> bool:
        status = self.get_state(self.peblar_status_entity)
        if status is None or status.lower() in ["unavailable", "unknown", "available"]:
            return False
        return status.lower() in ["preparing", "charging", "suspendedev", "suspendedevse", "finishing"]

    def _ampere_naar_kw(self, ampere: float) -> float:
        if self.FASEN == 1:
            return (ampere * self.SPANNING_V) / 1000
        elif self.FASEN == 3:
            return (ampere * self.SPANNING_V * 3) / 1000
        else:
            self.error(f"Ongeldig aantal fasen: {self.FASEN}.")
            return self.MAX_PAAL_KW

    def _kw_naar_ampere(self, vermogen_kw: float) -> float:
        return (vermogen_kw * 1000.0) / (self.SPANNING_V * 3.0)

    def _laad_verbruikshistorie(self):
        nu = datetime.datetime.now(tz.UTC)
        start = nu - datetime.timedelta(days=30)
        try:
            data = self.get_history(entity_id=self.verbruik_huis_entity, start_time=start, end_time=nu)
            if not data:
                self.warning("Geen verbruikshistorie beschikbaar, gebruik fallback.")
                self.db_cache_loaded = True
                return
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                records = data[0]
            elif isinstance(data, list):
                records = data
            else:
                records = [data]
            grouped = {}
            for s in records:
                try:
                    ts = self._extract_timestamp(s)
                    if ts is None:
                        continue
                    val = float(s["state"])
                    key = (ts.hour, ts.minute // 15)
                    grouped.setdefault(key, []).append(val)
                except (ValueError, KeyError, TypeError):
                    continue
            for key, vals in grouped.items():
                self.db_cache[key] = max(self.MIN_VERBRUIK_KW, sum(vals) / len(vals))
            self.db_cache_loaded = True
            self.log(f"Verbruikshistorie geladen: {len(self.db_cache)} kwartierblokken.")
        except Exception as e:
            self.error(f"Fout bij laden verbruikshistorie: {self._sanitize_error(e)}")
            self.db_cache_loaded = True

    def get_db_historie(self, uur: int, minuut: int) -> float:
        if not self.db_cache_loaded:
            self._laad_verbruikshistorie()
        kwartier_blok = minuut // 15
        cache_key = (uur, kwartier_blok)
        return self.db_cache.get(cache_key, self.FALLBACK_VERBRUIK_KW)

    def _parse_iso_to_utc(self, value: str) -> datetime.datetime:
        value = value.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.UTC)
        return dt.astimezone(tz.UTC)

    def _build_forecast_dict(self):
        self.forecast_dict = {}
        solcast_today = self.get_state(self.solcast_today_entity, attribute="all")
        solcast_tomorrow = self.get_state(self.solcast_tomorrow_entity, attribute="all")
        if not solcast_today and not solcast_tomorrow:
            self.warning("Geen Solcast forecasts beschikbaar.")
            return
        fc = []
        if solcast_today:
            fc.extend(solcast_today.get("attributes", {}).get("forecasts", []))
        if solcast_tomorrow:
            fc.extend(solcast_tomorrow.get("attributes", {}).get("forecasts", []))
        for f in fc:
            try:
                period_end = self._parse_iso_to_utc(f["period_end"])
                period_start = period_end - datetime.timedelta(minutes=30)
                pv_estimate = f.get("pv_estimate", 0.0)
                for i in range(2):
                    kwartier_start = period_start + datetime.timedelta(minutes=i * 15)
                    self.forecast_dict[kwartier_start] = pv_estimate / 2.0
            except Exception as e:
                self.error(f"Fout in forecast dictionary: {self._sanitize_error(e)}")

    def _build_prijs_dict(self):
        self.prijs_dict = {}
        p = self.get_state(self.epex_prijs_entity, attribute="all")
        if not p:
            self.warning("Geen EPEX prijzen beschikbaar, gebruik fallback.")
            return
        raw = p.get("attributes", {}).get("raw_today", []) + p.get("attributes", {}).get("raw_tomorrow", [])
        for r in raw:
            try:
                start = self._parse_iso_to_utc(r["start"])
                prijs = float(r["value"]) * self.EPEX_UNIT_MULTIPLIER
                if prijs < self.CRITICAL_PRICE_THRESHOLD:
                    self.log(f"Negatieve prijs: €{prijs:.2f} voor {start}")
                    prijs = self.bereken_totaal_tarief(prijs)
                    self.log(f"   Gecorrigeerd (incl. belastingen): €{prijs:.4f}")
                self.prijs_dict[start] = prijs
            except Exception as e:
                self.error(f"Fout bij parsen EPEX prijs: {self._sanitize_error(e)}")

    def bereken_laadplan(self):
        with self._lock:
            try:
                if self.planning_geannuleerd:
                    self.log("Planning is geannuleerd, geen herberekening.")
                    return
                if not self._check_kritieke_sensoren():
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return
                self._clear_plan()
                tijd_str = self.get_state(self.vertrektijd_entity)
                if tijd_str is None:
                    self.error("Vertrektijd is None! Stoppen.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                attrs = self.get_state(self.vertrektijd_entity, attribute="all")
                has_date = attrs.get("attributes", {}).get("has_date", False)
                has_time = attrs.get("attributes", {}).get("has_time", False)

                # Parse vertrektijd (datum + tijd of alleen tijd)
                if has_date and has_time:
                    lokale_nu = datetime.datetime.now(self._local_tz)
                    try:
                        vertrektijd_local = datetime.datetime.strptime(tijd_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._local_tz)
                    except ValueError:
                        try:
                            vertrektijd_local = datetime.datetime.fromisoformat(tijd_str).replace(tzinfo=self._local_tz)
                        except ValueError:
                            self.error(f"Kan vertrektijd niet parsen: {tijd_str}")
                            self._set_desired_state("Pure solar", False, 0.0)
                            self._reset_laadtracking()
                            self._save_persistent_data()
                            return
                    vertrektijd = vertrektijd_local.astimezone(tz.UTC)
                elif has_time:
                    lokale_nu = datetime.datetime.now(self._local_tz)
                    tijd_obj = datetime.datetime.strptime(tijd_str, "%H:%M:%S").time()
                    vertrektijd_local = lokale_nu.replace(hour=tijd_obj.hour, minute=tijd_obj.minute, second=0, microsecond=0)
                    if vertrektijd_local <= lokale_nu:
                        vertrektijd_local += datetime.timedelta(days=1)
                    vertrektijd = vertrektijd_local.astimezone(tz.UTC)
                else:
                    self.error("Vertrektijd heeft geen datum/tijd!")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                nu = datetime.datetime.now(tz.UTC)
                if vertrektijd <= nu:
                    self.log("Vertrektijd in verleden. Standby.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                if not self._is_auto_verbonden():
                    self.log("Geen auto verbonden. Standby.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                # Bereken doel SoC en netto behoefte
                gewenste_km = self.get_sensor_float(self.gewenste_km_entity)
                huidig_bereik = self.get_sensor_float(self.bereik_entity)
                huidige_soc = self.get_sensor_float(self.soc_entity)
                if None in [gewenste_km, huidig_bereik, huidige_soc]:
                    self.error("Kritieke sensoren ontbreken!")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                if (self.vorige_soc is not None and abs(huidige_soc - self.vorige_soc) > self.MAX_SOC_JUMP_PER_QUARTER):
                    self.error(f"Onrealistische SoC sprong: {self.vorige_soc}% -> {huidige_soc}%!")
                    self._send_push_notification("FordPass Fout", f"Onrealistische SoC sprong: {self.vorige_soc}% -> {huidige_soc}%")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                km_per_procent = (huidig_bereik / huidige_soc if huidige_soc > 0 and huidig_bereik > 0 else self.FALLBACK_KM_PER_PERCENT)
                doel_soc = max(self.MIN_SOC_SAFETY, min(100.0, (gewenste_km / km_per_procent) + self.VEILIGHEIDSMARGE_PERCENT))
                netto_behoefte_kwh = max(0.0, ((doel_soc - huidige_soc) / 100.0) * self.ACCU_CAP_KWH)
                self.log(f"SoC: {huidige_soc}% -> {doel_soc:.1f}% (+{self.VEILIGHEIDSMARGE_PERCENT}% marge). Netladen behoefte: {netto_behoefte_kwh:.2f} kWh")

                huidige_energie = self.get_sensor_float(self.peblar_energie_entity)
                if huidige_energie is None:
                    self.error("Energie sensor ongeldig!")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                if self.initiele_energie is None:
                    self.initiele_energie = max(0.0, huidige_energie)
                    self.cumulatief_geladen_kwh = 0.0
                    self.sessie_actief = True
                    self.vorige_soc = huidige_soc
                    self.log(f"Nieuwe sessie: initiele energie = {self.initiele_energie:.2f} kWh, SoC = {huidige_soc}%")

                laad_behoefte_kwh = netto_behoefte_kwh / self.LAADRENDEMENT
                self.sessie_doel_kwh = laad_behoefte_kwh
                self.doel_soc = doel_soc
                self.vorige_soc = huidige_soc

                # Bouw matrix kwartieren
                self._build_forecast_dict()
                self._build_prijs_dict()
                self.matrix_kwartieren = []
                nu_rounded = nu.replace(second=0, microsecond=0, tzinfo=tz.UTC)
                slot_start = nu_rounded.replace(minute=(nu_rounded.minute // 15) * 15)

                while slot_start < vertrektijd:
                    slot_end = slot_start + datetime.timedelta(minutes=15)
                    remaining_to_vertrek = (vertrektijd - slot_start).total_seconds()
                    if remaining_to_vertrek <= 0:
                        break

                    if slot_start < nu:
                        available_end = min(slot_end, vertrektijd)
                        slot_duration_sec = max(0, (available_end - nu).total_seconds())
                    else:
                        slot_duration_sec = min(self.SECONDS_PER_QUARTER, remaining_to_vertrek)
                    duration_hours = slot_duration_sec / 3600.0

                    zon_kwh_full = self.forecast_dict.get(slot_start, 0.0)
                    zon_kwh = zon_kwh_full * (duration_hours / self.QUARTER_HOURS)
                    verbruik_kw = self.get_db_historie(slot_start.hour, slot_start.minute)
                    verbruik_kwh = verbruik_kw * duration_hours

                    pv_kw = zon_kwh / duration_hours if duration_hours > 0 else 0.0
                    base_import_kw = max(0.0, verbruik_kw - pv_kw)
                    solar_surplus_kw = max(0.0, pv_kw - verbruik_kw)
                    zon_laad_kw = min(self.MAX_PAAL_KW, solar_surplus_kw)
                    laadbare_zon_kwh = zon_laad_kw * duration_hours

                    if laadbare_zon_kwh < self.TOLERANCE or zon_laad_kw < (self.MIN_LAADVERMOGEN_ZON_KW / self.QUARTER_HOURS * duration_hours):
                        laadbare_zon_kwh = 0.0
                        zon_laad_kw = 0.0

                    rest_paal_kw = self.MAX_PAAL_KW - zon_laad_kw
                    max_net_kw = min(rest_paal_kw, max(0.0, self.MAX_GROEP_KW - base_import_kw))
                    max_net_kwh_kwartier = max_net_kw * duration_hours

                    base_prijs = self.prijs_dict.get(slot_start, None)
                    if base_prijs is None:
                        base_prijs = self._get_fallback_prijs(slot_start)
                        self.log(f"Geen EPEX prijs voor {slot_start}, gebruik fallback: €{base_prijs:.2f}")
                    if base_prijs >= self.CRITICAL_PRICE_THRESHOLD:
                        base_prijs = self.bereken_totaal_tarief(base_prijs)

                    self.matrix_kwartieren.append({
                        "tijd": slot_start,
                        "duration_hours": duration_hours,
                        "laadbare_zon_kwh": laadbare_zon_kwh,
                        "max_net_kwh": max_net_kwh_kwartier,
                        "net_prijs": base_prijs,
                    })
                    slot_start = slot_end

                # Plan zonladen en netladen
                if netto_behoefte_kwh > self.TOLERANCE:
                    totale_capaciteit = sum(u["laadbare_zon_kwh"] + u["max_net_kwh"] for u in self.matrix_kwartieren)
                    if totale_capaciteit < netto_behoefte_kwh - self.TOLERANCE:
                        self.error(f"Onvoldoende capaciteit: {totale_capaciteit:.2f} kWh beschikbaar.")
                        self._set_desired_state("Pure solar", False, 0.0)
                        self._reset_laadtracking()
                        self._save_persistent_data()
                        return
                    if not self._bereken_geintegreerd_plan(netto_behoefte_kwh):
                        self._plan_zo_veel_mogelijk(netto_behoefte_kwh)
                else:
                    self._bereken_zonladen_plan()

                if self.plan:
                    start = min(self.plan.keys()).strftime("%Y-%m-%d %H:%M")
                    end = max(self.plan.keys()).strftime("%Y-%m-%d %H:%M")
                    zon_total = sum(v['zon_kwh'] for v in self.plan.values())
                    net_total = sum(v['net_kwh'] for v in self.plan.values())
                    self.log(f"Plan: {start}–{end} | Zon: {zon_total:.2f} kWh | Net: {net_total:.2f} kWh")
                else:
                    self.log("Geen netladen nodig, alleen zonladen.")

                self.last_plan_calculation = nu
                self.plan_valid_until = nu + datetime.timedelta(minutes=self.PLAN_VALIDITY_MINUTES)
                self._save_persistent_data()
                self._update_graph_data()

            except Exception as e:
                self.error(f"Fout in bereken_laadplan: {self._sanitize_error(e)}")
                self._clear_plan()
                self._set_desired_state("Pure solar", False, 0.0)
                self._reset_laadtracking()
                self._save_persistent_data()

    def _bereken_zonladen_plan(self):
        with self._lock:
            self.plan = {}
            for u in self.matrix_kwartieren:
                if u["laadbare_zon_kwh"] > self.TOLERANCE:
                    self.plan[u["tijd"]] = {"zon_kwh": u["laadbare_zon_kwh"], "net_kwh": 0.0}

    def _bereken_geintegreerd_plan(self, laad_behoefte_kwh: float) -> bool:
        with self._lock:
            self.plan = {}
            resterend = laad_behoefte_kwh
            for u in self.matrix_kwartieren:
                if resterend <= self.TOLERANCE:
                    break
                if u["laadbare_zon_kwh"] > self.TOLERANCE:
                    kwh = u["laadbare_zon_kwh"]
                    if kwh > self.TOLERANCE:
                        self.plan[u["tijd"]] = {"zon_kwh": kwh, "net_kwh": 0.0}
                        resterend = max(0.0, resterend - kwh)
            if resterend > self.TOLERANCE:
                return self._plan_net_kwartieren_aaneengesloten(resterend)
            return True

    def _plan_net_kwartieren_aaneengesloten(self, laad_behoefte_kwh: float) -> bool:
        with self._lock:
            best_start_idx = 0
            min_window_cost = float("inf")
            best_end_idx = len(self.matrix_kwartieren)

            for start_idx in range(len(self.matrix_kwartieren)):
                gevuld_kwh = 0.0
                window_cost = 0.0
                idx = 0
                while (gevuld_kwh < laad_behoefte_kwh - self.TOLERANCE and
                       start_idx + idx < len(self.matrix_kwartieren)):
                    u = self.matrix_kwartieren[start_idx + idx]
                    available_net_kwh = u["max_net_kwh"]
                    if available_net_kwh <= self.TOLERANCE:
                        idx += 1
                        continue
                    kwh = min(available_net_kwh, laad_behoefte_kwh - gevuld_kwh)
                    gevuld_kwh += kwh
                    window_cost += u["net_prijs"] * kwh
                    idx += 1

                if (gevuld_kwh >= laad_behoefte_kwh - self.TOLERANCE and
                    window_cost < min_window_cost):
                    min_window_cost = window_cost
                    best_start_idx = start_idx
                    best_end_idx = start_idx + idx

                if idx > 0 and self.DEBUG_LOGGING:
                    self.log(f"[DEBUG] Window {start_idx}: {gevuld_kwh:.2f} kWh, €{window_cost:.2f}")

            if min_window_cost == float("inf"):
                return False

            resterend = laad_behoefte_kwh
            for u in self.matrix_kwartieren[best_start_idx:best_end_idx]:
                if resterend <= self.TOLERANCE:
                    break
                available_net_kwh = u["max_net_kwh"]
                if available_net_kwh <= self.TOLERANCE:
                    continue
                kwh = min(available_net_kwh, resterend)
                if kwh > self.TOLERANCE:
                    if u["tijd"] in self.plan:
                        self.plan[u["tijd"]]["net_kwh"] = kwh
                    else:
                        self.plan[u["tijd"]] = {"zon_kwh": 0.0, "net_kwh": kwh}
                    resterend -= kwh

            if self.DEBUG_LOGGING:
                self.log(f"[DEBUG] Best window: {best_start_idx}-{best_end_idx}, €{min_window_cost:.2f}")
            return True

    def _plan_zo_veel_mogelijk(self, laad_behoefte_kwh: float):
        with self._lock:
            sorted_quarters = sorted(self.matrix_kwartieren, key=lambda u: u["net_prijs"])
            resterend = laad_behoefte_kwh
            for u in sorted_quarters:
                if resterend <= self.TOLERANCE:
                    break
                total_kwh = u["max_net_kwh"] + u["laadbare_zon_kwh"]
                kwh = min(total_kwh, resterend)
                if kwh > self.TOLERANCE:
                    zon = min(u["laadbare_zon_kwh"], kwh)
                    net = kwh - zon
                    if u["tijd"] in self.plan:
                        self.plan[u["tijd"]]["zon_kwh"] += zon
                        self.plan[u["tijd"]]["net_kwh"] += net
                    else:
                        self.plan[u["tijd"]] = {"zon_kwh": zon, "net_kwh": net}
                    resterend -= kwh

    def voer_schakeling_uit(self):
        with self._lock:
            try:
                if self.planning_geannuleerd:
                    self.log("Planning geannuleerd, geen schakeling.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    return

                nu = datetime.datetime.now(tz.UTC)
                huidige_soc = self.get_sensor_float(self.soc_entity)
                huidige_energie = self.get_sensor_float(self.peblar_energie_entity)

                if None in [huidige_soc, huidige_energie] or not self._check_kritieke_sensoren():
                    self.error("Kritieke sensoren ontbreken of onbeschikbaar!")
                    self._send_push_notification("Sensorstoring", "Kritieke sensor(en) onbeschikbaar. Sessie gestopt.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                # SoC sprong check
                if (self.vorige_soc is not None and huidige_soc is not None and
                    abs(huidige_soc - self.vorige_soc) > self.MAX_SOC_JUMP_PER_QUARTER):
                    self.error(f"Onrealistische SoC sprong: {self.vorige_soc}% -> {huidige_soc}%!")
                    self._send_push_notification("FordPass Fout", f"Onrealistische SoC sprong: {self.vorige_soc}% -> {huidige_soc}%")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                if huidige_soc is not None and not (0 <= huidige_soc <= 100):
                    self.error(f"Ongeldige SoC-waarde: {huidige_soc}%")
                    self._send_push_notification("Ongeldige SoC", f"Ongeldige SoC-waarde: {huidige_soc}%")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                huidig_kwartier = nu.replace(second=0, microsecond=0, minute=(nu.minute // 15) * 15, tzinfo=tz.UTC)

                # Herbereken als matrix verouderd is
                if (not self.matrix_kwartieren or self.plan_valid_until is None or nu >= self.plan_valid_until):
                    self.log("Matrix verouderd of plan verlopen, herberekenen...")
                    self.bereken_laadplan()
                    huidig_kwartier = nu.replace(second=0, microsecond=0, minute=(nu.minute // 15) * 15, tzinfo=tz.UTC)

                if not self._is_auto_verbonden():
                    self.log("Auto niet verbonden. Standby.")
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._save_persistent_data()
                    return

                # Tracking
                if self.initiele_energie is not None:
                    self.cumulatief_geladen_kwh = max(0.0, huidige_energie - self.initiele_energie)
                resterend = max(0.0, self.sessie_doel_kwh - self.cumulatief_geladen_kwh)

                # Stopcondities
                if resterend <= self.TOLERANCE and self.sessie_actief:
                    actueel = next((u for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), None)
                    if actueel and actueel["laadbare_zon_kwh"] > self.TOLERANCE:
                        self.log("Netladen klaar, maar zonladen nog mogelijk.")
                    else:
                        self.log(f"kWh-doel bereikt ({self.cumulatief_geladen_kwh:.2f} kWh). Sessie afsluiten.")
                        self._send_push_notification("kWh-Doel Bereikt", f"kWh-doel van {self.sessie_doel_kwh:.2f} kWh bereikt.")
                        self._set_desired_state("Pure solar", False, 0.0)
                        self._reset_laadtracking()
                        self._save_persistent_data()
                        return

                switch_state = self.get_state(self.peblar_switch_entity)
                if switch_state in [None, "unavailable", "unknown"]:
                    self.error("Switch niet beschikbaar!")
                    return

                # Anti-pendel: minimale draaitijd
                current_power_on = (switch_state == "on")
                if current_power_on and self.last_power_on_time is not None:
                    run_duration = (nu - self.last_power_on_time).total_seconds() / 60
                    if run_duration < self.MIN_RUN_MINUTES:
                        self.log(f"ANTI-PENDEL: Paal draait pas {run_duration:.1f} min (< {self.MIN_RUN_MINUTES} min). Doorgaan.")
                        return

                # Haal plan voor huidige kwartier
                plan = self.plan.get(huidig_kwartier, {"zon_kwh": 0.0, "net_kwh": 0.0})
                zon_kwh = plan.get("zon_kwh", 0.0)
                net_kwh = plan.get("net_kwh", 0.0)
                self.vorige_soc = huidige_soc

                # Track power on/off tijd
                if current_power_on and self.last_power_on_time is None:
                    self.last_power_on_time = nu
                elif not current_power_on:
                    self.last_power_on_time = None

                # NOOD: SoC te laag
                if huidige_soc < self.MIN_SOC_SAFETY and 0 <= huidige_soc <= 100:
                    self.log(f"NOOD: SoC {huidige_soc}% < {self.MIN_SOC_SAFETY}%. Maximaal laden.")
                    self._set_desired_state("Default", True, self.MAX_PAAL_KW)
                # Netladen
                elif net_kwh > self.TOLERANCE:
                    total_kwh = zon_kwh + net_kwh
                    duration = next((u["duration_hours"] for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), self.QUARTER_HOURS)
                    kw = total_kwh / duration if duration > 0 else 0.0
                    if kw < self.MIN_LAADVERMOGEN_KW:
                        kw = self.MIN_LAADVERMOGEN_KW
                    else:
                        kw = min(self.MAX_PAAL_KW, kw)
                    self._set_desired_state("Default", True, kw)
                # Zonladen
                elif zon_kwh > self.TOLERANCE:
                    duration = next((u["duration_hours"] for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), self.QUARTER_HOURS)
                    kw = zon_kwh / duration if duration > 0 else 0.0
                    self._set_desired_state("Pure solar", True, kw)
                # Zonladen altijd mogelijk
                else:
                    actueel = next((u for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), None)
                    if actueel and actueel["laadbare_zon_kwh"] > self.TOLERANCE:
                        duration = actueel.get("duration_hours", self.QUARTER_HOURS)
                        kw = actueel["laadbare_zon_kwh"] / duration if duration > 0 else 0.0
                        self._set_desired_state("Pure solar", True, kw)
                    else:
                        self._set_desired_state("Pure solar", False, 0.0)

            except Exception as e:
                self.error(f"Fout in voer_schakeling_uit: {self._sanitize_error(e)}")
                self._set_desired_state("Pure solar", False, 0.0)
                self._reset_laadtracking()
                self._save_persistent_data()

    def _update_graph_data(self):
        try:
            if not self.matrix_kwartieren:
                return
            graph_data = {
                "tijden": [],
                "epex_tarieven": [],
                "totaal_tarieven": [],
                "zon_opwek": [],
                "huis_verbruik": [],
                "laadplanning_zon": [],
                "laadplanning_net": [],
                "metadata": {
                    "energiebelasting": self.ENERGIEBELASTING_PER_KWH,
                    "leverancierskosten": self.LEVERANCIERSKOSTEN_PER_KWH,
                    "btw": self.BTW_PERCENTAGE,
                    "laadrendement": self.LAADRENDEMENT,
                }
            }
            for u in self.matrix_kwartieren:
                tijd_str = u["tijd"].strftime("%Y-%m-%d %H:%M")
                graph_data["tijden"].append(tijd_str)
                epex_prijs = u["net_prijs"]
                graph_data["epex_tarieven"].append(round(epex_prijs, 4))
                if epex_prijs >= self.CRITICAL_PRICE_THRESHOLD:
                    basis_prijs = epex_prijs / (1 + (self.BTW_PERCENTAGE / 100)) - self.ENERGIEBELASTING_PER_KWH - self.LEVERANCIERSKOSTEN_PER_KWH
                else:
                    basis_prijs = epex_prijs
                graph_data["totaal_tarieven"].append(round(self.bereken_totaal_tarief(basis_prijs), 4))
                graph_data["zon_opwek"].append(round(u["laadbare_zon_kwh"], 2))
                verbruik_kw = self.get_db_historie(u["tijd"].hour, u["tijd"].minute)
                graph_data["huis_verbruik"].append(round(verbruik_kw, 2))
                plan_entry = self.plan.get(u["tijd"], {"zon_kwh": 0.0, "net_kwh": 0.0})
                graph_data["laadplanning_zon"].append(round(plan_entry.get("zon_kwh", 0.0), 2))
                graph_data["laadplanning_net"].append(round(plan_entry.get("net_kwh", 0.0), 2))
            self.set_state(self.GRAPH_DATA_ENTITY, state=json.dumps(graph_data))
        except Exception as e:
            self.error(f"Fout bij updaten grafiek data: {self._sanitize_error(e)}")

    def _set_desired_state(self, modus: str, power_on: bool, vermogen_kw: float):
        with self._lock:
            current_mode = self.get_state(self.peblar_mode_entity)
            current_switch = self.get_state(self.peblar_switch_entity)
            current_ampere = self.get_sensor_float(self.peblar_laadlimiet_entity)

            # Check of wijziging nodig is
            needs_mode_change = (current_mode != modus)
            needs_switch_change = (current_switch == "on") != power_on
            needs_ampere_change = False

            if power_on and modus == "Default":
                desired_ampere = self._kw_naar_ampere(vermogen_kw)
                desired_ampere = round(max(self.MIN_AMPERE, min(self.MAX_AMPERE, desired_ampere)))
                needs_ampere_change = (current_ampere is None or abs(current_ampere - desired_ampere) > self.AMPERE_TOLERANCE)

            if needs_mode_change or needs_switch_change or needs_ampere_change:
                self.log(f"Nieuwe staat: {modus}, power={power_on}, vermogen={vermogen_kw:.2f} kW")

            self._desired_mode = modus
            self._desired_power_on = power_on
            self._desired_kw = vermogen_kw
            self._control_generation += 1
            self._cancel_pending_control()
            self._reconcile_peblar_state(self._control_generation)

    def _cancel_pending_control(self):
        with self._lock:
            if self._pending_control_timer is not None:
                self.cancel_timer(self._pending_control_timer)
                self._pending_control_timer = None

    def _reconcile_peblar_state(self, generation: int):
        with self._lock:
            if generation != self._control_generation:
                return
            if not self._desired_power_on:
                self._force_switch_off()
                return

            current_mode = self.get_state(self.peblar_mode_entity)
            current_switch = self.get_state(self.peblar_switch_entity)
            if current_switch in [None, "unavailable", "unknown"]:
                self.error("Switch niet beschikbaar!")
                return
            if current_mode in [None, "unavailable", "unknown"]:
                self.error("Mode niet beschikbaar!")
                return

            # Stap 1: Modus instellen
            if current_mode != self._desired_mode:
                if self._check_service_call_delay(self.last_mode_call):
                    self.call_service("select/select_option",
                                     entity_id=self.peblar_mode_entity,
                                     option=self._desired_mode)
                    self.last_mode_call = datetime.datetime.now(tz.UTC)
                    self._pending_control_timer = self.run_in(
                        self._reconcile_peblar_state_callback, 2, generation=generation
                    )
                else:
                    delay = self._remaining_delay(self.last_mode_call)
                    self._pending_control_timer = self.run_in(
                        self._reconcile_peblar_state_callback, delay + 0.5, generation=generation
                    )
                return

            # Stap 2: Laadlimiet instellen (alleen in Default modus)
            if self._desired_mode == "Default" and self._desired_kw > 0:
                if not self._set_laadvermogen(self._desired_kw):
                    delay = self._remaining_delay(self.last_laadlimiet_call)
                    self._pending_control_timer = self.run_in(
                        self._reconcile_peblar_state_callback, delay + 0.5, generation=generation
                    )
                    return
                if self.last_laadlimiet_call is not None:
                    elapsed = (datetime.datetime.now(tz.UTC) - self.last_laadlimiet_call).total_seconds()
                    if elapsed < 2:
                        self._pending_control_timer = self.run_in(
                            self._reconcile_peblar_state_callback, 2 - elapsed, generation=generation
                        )
                        return

            # Stap 3: Switch inschakelen
            if current_switch != "on":
                if self._check_service_call_delay(self.last_switch_call):
                    self._force_switch_on()
                else:
                    delay = self._remaining_delay(self.last_switch_call)
                    self._pending_control_timer = self.run_in(
                        self._reconcile_peblar_state_callback, delay + 0.5, generation=generation
                    )

    def _reconcile_peblar_state_callback(self, kwargs):
        generation = kwargs.get("generation", 0)
        with self._lock:
            self._pending_control_timer = None
            self._reconcile_peblar_state(generation)

    def _force_switch_off(self):
        current_switch = self.get_state(self.peblar_switch_entity)
        if current_switch == "on":
            self.call_service("switch/turn_off", entity_id=self.peblar_switch_entity)
            self.last_switch_call = datetime.datetime.now(tz.UTC)
            self.last_power_on_time = None
            self.log("Peblar uitgeschakeld.")

    def _force_switch_on(self):
        current_switch = self.get_state(self.peblar_switch_entity)
        if current_switch != "on":
            self.call_service("switch/turn_on", entity_id=self.peblar_switch_entity)
            self.last_switch_call = datetime.datetime.now(tz.UTC)
            self.last_power_on_time = datetime.datetime.now(tz.UTC)
            self.log("Peblar ingeschakeld.")

    def _set_laadvermogen(self, vermogen_kw: float) -> bool:
        ampere = self._kw_naar_ampere(vermogen_kw)
        ampere = round(max(self.MIN_AMPERE, min(self.MAX_AMPERE, ampere)))
        current_ampere = self.get_sensor_float(self.peblar_laadlimiet_entity)
        if current_ampere is None:
            self.error("Laadlimiet sensor niet beschikbaar!")
            return False
        if abs(current_ampere - ampere) <= self.AMPERE_TOLERANCE:
            return True
        if not self._check_service_call_delay(self.last_laadlimiet_call):
            return False
        self.call_service("number/set_value",
                          entity_id=self.peblar_laadlimiet_entity,
                          value=ampere)
        self.log(f"Laadlimiet: {ampere:.1f} A (~{vermogen_kw:.1f} kW)")
        self.last_laadlimiet_call = datetime.datetime.now(tz.UTC)
        return True