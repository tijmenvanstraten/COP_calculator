import hassapi as hass
import datetime
import threading
import json
import math
import re
from typing import Dict, Optional, Any
from dateutil import tz


class PlanningAbort(Exception):
    """Planning of plancorrectie kon niet worden afgerond (verwachte, afgehandelde situatie)."""

    def __init__(self, reden: str, *, fout: bool = True,
                 wacht_forecast: bool = False, afgekoppeld: bool = False):
        super().__init__(reden)
        self.reden = reden
        self.fout = fout
        self.wacht_forecast = wacht_forecast
        self.afgekoppeld = afgekoppeld


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
    FALLBACK_NET_PRIJS = 0.4598
    LAADRENDEMENT = 0.90
    QUARTER_HOURS = 0.25
    PLAN_CORRECTIE_TOLERANTIE_KWH = 0.25
    AMPERE_TOLERANCE = 0.1
    PLAN_VALIDITY_MINUTES = 30
    SERVICE_CALL_DELAY = 5
    MAX_GROEP_KW = 17.0
    MIN_AMPERE = 6.0
    MAX_AMPERE = 16.0
    MAX_SOC_JUMP_PER_QUARTER = 10.0
    MIN_RUN_MINUTES = 30
    MIN_OFF_MINUTES = 10
    MAX_SENSOR_STORING_KWARTIEREN = 3
    SWITCH_COMMAND_GRACE_SECONDS = 30
    MIN_LAADVERMOGEN_ZON_KW = 1.38
    GRAPH_ATTRIBUTE_MAX_BYTES = 16000
    PROGRESS_UPDATE_DEBOUNCE_SECONDS = 60
    PROGRESS_UPDATE_MIN_DELTA_KWH = 0.01

    # Peblar CP-status (sensor.peblar_ev_charger_status, HA Peblar-integratie).
    # Overschrijfbaar via apps.yaml: status_verbonden / status_losgekoppeld / status_storing
    STATUS_VERBONDEN = ("charging", "suspended")
    STATUS_LOSGEKOPPELD = ("no_ev_connected",)
    STATUS_STORING = ("error", "fault", "invalid")

    # Energie core forecast
    FORECAST_MAX_AGE_MINUTES = 45

    # Persistentie instellingen
    PERSISTENCE_ENTITY = "input_text.peblar_horizon_planner_persistent_data"
    PERSISTENCE_ENABLED = True
    GRAPH_DATA_ENTITY = "sensor.peblar_horizon_planner_graph_data"
    PLAN_STATUS_ENTITY = "sensor.peblar_horizon_planner_plan_status"
    PLAN_BEREKEN_BUTTON = "input_button.ford_capri_ev_start_laadplan_berekening"
    PLAN_UITVOEREN_BOOLEAN = "input_boolean.ford_capri_ev_uitvoeren_laadplan"
    ANNULEER_SERVICE = "peblar_horizon_planner/annuleer_planning"

    # ===== ENTITY CONFIGURATIE =====
    DEFAULT_ENTITIES = {
        "soc_entity": "sensor.fordpass_wf0spbef7ssj42833_soc",
        "bereik_entity": "sensor.fordpass_wf0spbef7ssj42833_elveh",
        "peblar_energie_entity": "sensor.peblar_ev_charger_levenslange_energie",
        "vertrektijd_entity": "input_datetime.ford_capri_ev_charging_ready_setpoint",
        "gewenste_km_entity": "input_number.ford_capri_ev_gewenste_kilometers",
        "forecast_entity": "sensor.energie_core_forecast",
        "peblar_switch_entity": "switch.peblar_ev_charger_opladen",
        "peblar_mode_entity": "select.peblar_ev_charger_slim_laden",
        "peblar_laadlimiet_entity": "number.peblar_ev_charger_laadlimiet",
        "peblar_status_entity": "sensor.peblar_ev_charger_status",
        "push_notification_entity": "notify.mobile_app_iphone",
        "plan_bereken_button_entity": "input_button.ford_capri_ev_start_laadplan_berekening",
        "plan_uitvoeren_boolean_entity": "input_boolean.ford_capri_ev_uitvoeren_laadplan",
        "plan_status_entity": "sensor.peblar_horizon_planner_plan_status",
    }

    FASEN = 3
    SPANNING_V = 230.0
    LOCAL_TZ_NAME = "Europe/Amsterdam"

    def initialize(self):
        self.log("Peblar Horizon Planner v12 (core forecast) opgestart.")
        self._lock = threading.RLock()
        self._load_configuration()
        self._local_tz = tz.gettz(self.LOCAL_TZ_NAME)
        self.MIN_LAADVERMOGEN_KW = self._ampere_naar_kw(self.MIN_AMPERE)

        # Plan state
        self.plan = {}
        self.matrix_kwartieren = []
        self.last_plan_calculation = None
        self._plan_start_time = None
        self._plan_start_meterstand = None
        self.doel_soc = None
        self.sessie_doel_kwh = 0.0
        self.cumulatief_geladen_kwh = 0.0
        self.initiele_energie = None
        self.last_power_on_time = None
        self.last_power_off_time = None
        self.sessie_actief = False
        self.vorige_soc = None
        self.vorige_soc_energie = None
        self.last_mode_call = None
        self.last_switch_call = None
        self.last_laadlimiet_call = None
        self._desired_mode = "Pure solar"
        self._desired_power_on = False
        self._desired_kw = 0.0
        self._control_generation = 0
        self._pending_control_timer = None
        self._minimum_off_timer = None
        self._progress_update_timer = None
        self._sensor_storingen = 0
        self._last_switch_command_time = None
        self._graph_page_counts = {}
        self._last_progress_kwh = None
        self._last_switch_command_state = None
        self.planning_geannuleerd = False
        self._plan_status = "geen_plan"
        self._plan_message = None
        self._forecast_alarm_sent = False
        self._wacht_op_forecast = False
        self._forecast_status = None
        self._tarief_meta = {}
        self._gelogde_meldingen = {}

        # Laad persistente data
        self._load_persistent_data()
        self._register_services()
        self._schedule_kwartier_timer()

        # Input en goedkeuring
        self.listen_state(self._plan_berekenen_ingedrukt, self.PLAN_BEREKEN_BUTTON)
        self.listen_state(self._uitvoering_boolean_gewijzigd, self.PLAN_UITVOEREN_BOOLEAN)
        self.listen_state(self._planning_invoer_gewijzigd, self.vertrektijd_entity)
        self.listen_state(self._planning_invoer_gewijzigd, self.gewenste_km_entity)
        self.listen_state(self._auto_status_changed, self.peblar_status_entity)
        self.listen_state(self._peblar_switch_changed, self.peblar_switch_entity)
        self.listen_state(self._meterstand_changed, self.peblar_energie_entity)
        self.listen_state(self._forecast_updated, self.forecast_entity)

        self._set_execution_boolean(False)
        self._set_plan_status("geen_plan")
        self._set_desired_state("Pure solar", False, 0.0)

    def terminate(self):
        with self._lock:
            self._cancel_pending_control()
            self._cancel_minimum_off_timer()
            self._cancel_progress_update_timer()
            self._set_execution_boolean(False)
            self._set_desired_state("Pure solar", False, 0.0)

    def _register_services(self):
        self.listen_service(self._annuleer_planning_service, self.ANNULEER_SERVICE)

    def _set_plan_status(self, status: str, message: Optional[str] = None):
        if status != "in_uitvoering":
            self._cancel_progress_update_timer()
        self._plan_status = status
        self._plan_message = message
        doel_kwh = self._finite_nonnegative(self.sessie_doel_kwh)
        geladen_kwh = self._finite_nonnegative(self.cumulatief_geladen_kwh)
        initiele_energie = self._finite_nonnegative(self.initiele_energie)
        sessie_geldig = (
            status in ("concept", "in_uitvoering", "gepauzeerd")
            and self.sessie_actief
            and initiele_energie is not None
            and doel_kwh is not None
            and geladen_kwh is not None
        )
        if not sessie_geldig:
            doel_kwh = None
            geladen_kwh = None
            resterend = None
        else:
            resterend = max(0.0, doel_kwh - geladen_kwh)
        fallback_count = sum(
            1 for slot in self.matrix_kwartieren if slot.get("prijs_bron") == "fallback"
        )
        slot_count = len(self.matrix_kwartieren)
        self.set_state(
            self.PLAN_STATUS_ENTITY,
            state=status,
            attributes={
                "friendly_name": "Peblar laadplanstatus",
                "message": message,
                "doel_kwh": round(doel_kwh, 3) if doel_kwh is not None else None,
                "geladen_kwh": round(geladen_kwh, 3) if geladen_kwh is not None else None,
                "resterend_kwh": round(resterend, 3) if resterend is not None else None,
                "fallback_prijs_kwartieren": fallback_count,
                "fallback_prijs_percentage": round(100.0 * fallback_count / slot_count, 1) if slot_count else 0.0,
                "updated_at": datetime.datetime.now(tz.UTC).isoformat(),
            },
        )
        if not getattr(self, "matrix_kwartieren", []):
            self._update_graph_data()

    def _meterstand_changed(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if (
                self._plan_status != "in_uitvoering"
                or not self.sessie_actief
                or self._finite_nonnegative(self.initiele_energie) is None
            ):
                return
            huidige_energie = self.get_sensor_float(self.peblar_energie_entity)
            if huidige_energie is None:
                return
            cumulatief = max(0.0, huidige_energie - self.initiele_energie)
            if (
                self._last_progress_kwh is not None
                and abs(cumulatief - self._last_progress_kwh) < self.PROGRESS_UPDATE_MIN_DELTA_KWH
            ):
                return
            if self._progress_update_timer is None:
                self._progress_update_timer = self.run_in(
                    self._progress_update_callback,
                    self.PROGRESS_UPDATE_DEBOUNCE_SECONDS,
                )

    def _progress_update_callback(self, kwargs):
        with self._lock:
            self._progress_update_timer = None
            if (
                self._plan_status != "in_uitvoering"
                or not self.sessie_actief
                or self._finite_nonnegative(self.initiele_energie) is None
            ):
                return
            huidige_energie = self.get_sensor_float(self.peblar_energie_entity)
            if huidige_energie is None:
                return
            self.cumulatief_geladen_kwh = max(0.0, huidige_energie - self.initiele_energie)
            self._last_progress_kwh = self.cumulatief_geladen_kwh
            self._set_plan_status(self._plan_status, self._plan_message)

    def _cancel_progress_update_timer(self):
        timer = getattr(self, "_progress_update_timer", None)
        if timer is not None:
            self.cancel_timer(timer)
            self._progress_update_timer = None

    @staticmethod
    def _finite_nonnegative(value) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        return number

    def _set_execution_boolean(self, enabled: bool):
        desired = "on" if enabled else "off"
        current = str(self.get_state(self.PLAN_UITVOEREN_BOOLEAN) or "").lower()
        if current != desired:
            service = "input_boolean/turn_on" if enabled else "input_boolean/turn_off"
            self.call_service(service, entity_id=self.PLAN_UITVOEREN_BOOLEAN)

    def _plan_berekenen_ingedrukt(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self.planning_geannuleerd = False
            self._set_execution_boolean(False)
            self._set_desired_state("Pure solar", False, 0.0)
            self._set_plan_status("berekenen")
            self.last_plan_calculation = None
            self.bereken_laadplan()
            if self.last_plan_calculation is not None:
                self._set_plan_status("concept", "Wacht op goedkeuring via de uitvoerschakelaar.")
            elif self._wacht_op_forecast:
                self._set_plan_status("wacht_op_forecast", "Forecast beschikbaar? Bereken het plan opnieuw via de knop.")
            elif not self._is_auto_verbonden():
                self._set_plan_status("wacht_op_auto", "Sluit de auto aan en bereken het plan opnieuw.")
                self._set_execution_boolean(False)
            else:
                self._set_plan_status("fout", "Geen bruikbaar conceptplan berekend.")

    def _uitvoering_boolean_gewijzigd(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if str(new).lower() != "on":
                if self._plan_status == "in_uitvoering":
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._plan_start_time = None
                    self._plan_start_meterstand = None
                    self._set_plan_status("gepauzeerd", "Uitvoering gepauzeerd; zet de schakelaar aan om het plan te hervatten.")
                return

            if self._plan_status not in ("concept", "gepauzeerd") or not self.matrix_kwartieren:
                self._set_execution_boolean(False)
                self._set_plan_status("fout", "Er is geen geldig conceptplan om goed te keuren.")
                return
            if self.last_plan_calculation is None or (
                datetime.datetime.now(tz.UTC) - self.last_plan_calculation
                > datetime.timedelta(minutes=self.PLAN_VALIDITY_MINUTES)
            ):
                self._set_execution_boolean(False)
                self._set_plan_status("concept_verlopen", "Bereken een nieuw plan voordat je uitvoering start.")
                return
            if not self._is_auto_verbonden():
                self._set_execution_boolean(False)
                self._set_plan_status("wacht_op_auto", "Sluit de auto aan voordat je uitvoering start.")
                return

            huidige_energie = self.get_sensor_float(self.peblar_energie_entity)
            if huidige_energie is None:
                self._set_execution_boolean(False)
                self._set_plan_status("fout", "Laadenergiemeter is niet beschikbaar.")
                return

            nu = datetime.datetime.now(tz.UTC)
            self._plan_start_time = nu
            self._plan_start_meterstand = huidige_energie
            if self._finite_nonnegative(self.initiele_energie) is not None:
                self._last_progress_kwh = self.cumulatief_geladen_kwh
            # Bewuste start/hervatting door de gebruiker: geen minimale uit-tijd afwachten
            self.last_power_off_time = None
            self._cancel_minimum_off_timer()
            self._set_plan_status("in_uitvoering")
            self.voer_schakeling_uit()

    def _planning_invoer_gewijzigd(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            had_plan = bool(self.matrix_kwartieren)
            self._set_execution_boolean(False)
            self._set_desired_state("Pure solar", False, 0.0)
            self._clear_plan()
            self.last_plan_calculation = None
            status = "opnieuw_berekenen" if had_plan else "geen_plan"
            message = "Doel of vertrektijd gewijzigd; bereken een nieuw conceptplan." if had_plan else None
            self._set_plan_status(status, message)

    def _annuleer_planning_service(self, **kwargs):
        with self._lock:
            self.log("Planning geannuleerd via service call.")
            self.planning_geannuleerd = True
            self._set_execution_boolean(False)
            self._clear_plan()
            self._set_desired_state("Pure solar", False, 0.0)
            self._reset_laadtracking()
            self._set_plan_status("geannuleerd")
            self._save_persistent_data()
            self._send_push_notification("Planning Geannuleerd", "De laadplanning is geannuleerd.")

    def _load_configuration(self):
        for key, default in self.DEFAULT_ENTITIES.items():
            setattr(self, key, self.args.get(key, default))
        self.PLAN_BEREKEN_BUTTON = self.plan_bereken_button_entity
        self.PLAN_UITVOEREN_BOOLEAN = self.plan_uitvoeren_boolean_entity
        self.PLAN_STATUS_ENTITY = self.plan_status_entity
        self.FASEN = int(self.args.get("fasen", self.FASEN))
        self.SPANNING_V = float(self.args.get("spanning_v", self.SPANNING_V))
        self.PLAN_VALIDITY_MINUTES = int(self.args.get("plan_validity_minutes", self.PLAN_VALIDITY_MINUTES))
        self.SERVICE_CALL_DELAY = int(self.args.get("service_call_delay", self.SERVICE_CALL_DELAY))
        self.MAX_GROEP_KW = float(self.args.get("max_groep_kw", self.MAX_GROEP_KW))
        self.MIN_AMPERE = float(self.args.get("min_ampere", self.MIN_AMPERE))
        self.MAX_AMPERE = float(self.args.get("max_ampere", self.MAX_AMPERE))
        self.LAADRENDEMENT = float(self.args.get("laadrendement", self.LAADRENDEMENT))
        self.MAX_SOC_JUMP_PER_QUARTER = float(self.args.get("max_soc_jump", self.MAX_SOC_JUMP_PER_QUARTER))
        self.MIN_RUN_MINUTES = int(self.args.get("min_run_minutes", self.MIN_RUN_MINUTES))
        self.MIN_OFF_MINUTES = int(self.args.get("min_off_minutes", self.MIN_OFF_MINUTES))
        self.MAX_SENSOR_STORING_KWARTIEREN = max(1, int(self.args.get(
            "max_sensor_storing_kwartieren", self.MAX_SENSOR_STORING_KWARTIEREN)))
        self.STATUS_VERBONDEN = self._status_lijst("status_verbonden", self.STATUS_VERBONDEN)
        self.STATUS_LOSGEKOPPELD = self._status_lijst("status_losgekoppeld", self.STATUS_LOSGEKOPPELD)
        self.STATUS_STORING = self._status_lijst("status_storing", self.STATUS_STORING)
        self.MIN_LAADVERMOGEN_ZON_KW = float(self.args.get("min_laadvermogen_zon_kw", self.MIN_LAADVERMOGEN_ZON_KW))
        self.PERSISTENCE_ENTITY = self.args.get("persistence_entity", self.PERSISTENCE_ENTITY)
        self.PERSISTENCE_ENABLED = bool(self.args.get("persistence_enabled", self.PERSISTENCE_ENABLED))
        self.GRAPH_DATA_ENTITY = self.args.get("graph_data_entity", self.GRAPH_DATA_ENTITY)
        self.FORECAST_MAX_AGE_MINUTES = int(self.args.get("forecast_max_age_minutes", self.FORECAST_MAX_AGE_MINUTES))
        self.FALLBACK_NET_PRIJS = float(self.args.get("fallback_net_prijs", self.FALLBACK_NET_PRIJS))
        self.FALLBACK_VERBRUIK_KW = float(self.args.get("fallback_verbruik_kw", self.FALLBACK_VERBRUIK_KW))
        self.PLAN_CORRECTIE_TOLERANTIE_KWH = max(
            self.TOLERANCE,
            float(self.args.get("plan_correctie_tolerantie_kwh", self.PLAN_CORRECTIE_TOLERANTIE_KWH)))

    def _load_persistent_data(self):
        if not self.PERSISTENCE_ENABLED:
            return
        try:
            state = self.get_state(self.PERSISTENCE_ENTITY)
            if state is None or str(state).strip().lower() in ["", "unavailable", "unknown", "none"]:
                return
            data = json.loads(state)
            if not isinstance(data, dict):
                self.warning("Persistente data heeft een onverwacht formaat, genegeerd.")
                return
            with self._lock:
                doel_soc = data.get("doel_soc")
                self.doel_soc = float(doel_soc) if doel_soc is not None else None
                self.sessie_doel_kwh = float(data.get("sessie_doel_kwh", 0.0) or 0.0)
                self.cumulatief_geladen_kwh = float(data.get("cumulatief_geladen_kwh", 0.0) or 0.0)
                initiele_energie = data.get("initiele_energie")
                self.initiele_energie = float(initiele_energie) if initiele_energie is not None else None
                self.planning_geannuleerd = bool(data.get("planning_geannuleerd", False))
                # Sessie alleen hervatten als er een startmeterstand is om tegen te meten
                self.sessie_actief = bool(data.get("sessie_actief", False)) and self.initiele_energie is not None
                self._last_progress_kwh = self.cumulatief_geladen_kwh
                self._last_persist_payload = str(state)
            self.log(f"Persistente data geladen: sessie_actief={self.sessie_actief}, "
                     f"initiele_energie={self.initiele_energie}, geannuleerd={self.planning_geannuleerd}")
        except Exception as e:
            self.warning(f"Kon persistente data niet laden: {self._sanitize_error(e)}")

    def _save_persistent_data(self):
        if not self.PERSISTENCE_ENABLED:
            return
        try:
            data = {
                "doel_soc": round(self.doel_soc, 2) if self.doel_soc is not None else None,
                "sessie_doel_kwh": round(self.sessie_doel_kwh, 3),
                "cumulatief_geladen_kwh": round(self.cumulatief_geladen_kwh, 3),
                "initiele_energie": round(self.initiele_energie, 3) if self.initiele_energie is not None else None,
                "planning_geannuleerd": bool(self.planning_geannuleerd),
                "sessie_actief": bool(self.sessie_actief),
            }
            payload = json.dumps(data, separators=(",", ":"))
            if len(payload) > 255:
                self.error(f"Persistente data te lang voor input_text ({len(payload)} > 255 tekens), niet opgeslagen.")
                return
            if payload == getattr(self, "_last_persist_payload", None):
                return
            self.call_service("input_text/set_value", entity_id=self.PERSISTENCE_ENTITY, value=payload)
            self._last_persist_payload = payload
        except Exception as e:
            self.error(f"Kon persistente data niet opslaan: {self._sanitize_error(e)}")

    def _forecast_updated(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            state = self.get_state(self.forecast_entity, attribute="all")
            attrs = (state or {}).get("attributes", {}) or {}
            self._forecast_status = attrs.get("status")
            if self._forecast_status in ("degraded", "partial"):
                self._log_bij_verandering(
                    "forecast_status",
                    f"Forecast status '{self._forecast_status}': {attrs.get('waarschuwingen')}")
            else:
                self._log_bij_verandering("forecast_status", None)

    def _log_bij_verandering(self, sleutel: str, melding: Optional[str]):
        vorige = self._gelogde_meldingen.get(sleutel)
        if melding:
            if melding != vorige:
                self.warning(melding)
            self._gelogde_meldingen[sleutel] = melding
        elif vorige is not None:
            self.log(f"Melding opgelost: {vorige}")
            self._gelogde_meldingen.pop(sleutel, None)

    def _parse_iso_to_utc(self, value: Any) -> datetime.datetime:
        if isinstance(value, datetime.datetime):
            dt = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.UTC)
        return dt.astimezone(tz.UTC)

    def _lees_forecast(self) -> Optional[Dict[datetime.datetime, Dict[str, Any]]]:
        """Leest de matrix uit het 'forecast'-attribuut van sensor.energie_core_forecast."""
        state = self.get_state(self.forecast_entity, attribute="all")
        if not state or str(state.get("state", "")).lower() in ["unavailable", "unknown", "none"]:
            self.warning(f"Forecast sensor {self.forecast_entity} niet beschikbaar (core nog niet gestart?).")
            return None
        attrs = state.get("attributes", {}) or {}
        raw = attrs.get("forecast")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if not isinstance(raw, list) or not raw:
            self.error("Forecast attribuut ontbreekt of is leeg.")
            return None

        generated = attrs.get("generated_at") or state.get("state")
        try:
            leeftijd_min = (datetime.datetime.now(tz.UTC) - self._parse_iso_to_utc(generated)).total_seconds() / 60.0
        except (ValueError, TypeError):
            self.error(f"Ongeldige generated_at in forecast: {generated}")
            return None
        if leeftijd_min > self.FORECAST_MAX_AGE_MINUTES:
            self.error(f"Forecast is verouderd ({leeftijd_min:.0f} min > {self.FORECAST_MAX_AGE_MINUTES} min).")
            if not self._forecast_alarm_sent:
                self._forecast_alarm_sent = True
                self._send_push_notification("Forecast Verouderd",
                                             f"Energie core forecast is {leeftijd_min:.0f} minuten oud. Planning wordt niet (her)berekend.")
            return None
        self._forecast_alarm_sent = False

        self._forecast_status = attrs.get("status")
        if self._forecast_status in ("degraded", "partial"):
            self._log_bij_verandering("forecast_status",
                                      f"Forecast status '{self._forecast_status}': {attrs.get('waarschuwingen')}")
        else:
            self._log_bij_verandering("forecast_status", None)

        tarief = attrs.get("tarief")
        self._tarief_meta = dict(tarief) if isinstance(tarief, dict) else {}

        resultaat: Dict[datetime.datetime, Dict[str, Any]] = {}
        for item in raw:
            try:
                tijd = self._parse_iso_to_utc(item["tijd"])
                resultaat[tijd] = {
                    "pv_kw": max(0.0, float(item.get("pv_kw", 0.0))),
                    "verbruik_kw": max(0.0, float(item.get("verbruik_kw", self.FALLBACK_VERBRUIK_KW))),
                    "epex_prijs": float(item.get("epex_prijs", 0.0)),
                    "net_prijs": float(item["net_prijs"]),
                    "prijs_bron": str(item.get("prijs_bron", "epex")),
                }
            except (KeyError, ValueError, TypeError) as e:
                self.error(f"Ongeldig forecast-kwartier overgeslagen: {self._sanitize_error(e)}")
        if not resultaat:
            self.error("Forecast bevat geen bruikbare kwartieren.")
            return None
        return resultaat

    def _clear_plan(self):
        with self._lock:
            self.plan = {}
            self.matrix_kwartieren = []
            self._plan_start_time = None
            self._plan_start_meterstand = None
            self.last_plan_calculation = None
            self.doel_soc = None
            self.sessie_doel_kwh = 0.0

    def _reset_laadtracking(self):
        with self._lock:
            self._cancel_progress_update_timer()
            self.cumulatief_geladen_kwh = 0.0
            self.initiele_energie = None
            self._last_progress_kwh = None
            self.sessie_doel_kwh = 0.0
            self.sessie_actief = False
            self.vorige_soc = None
            self.vorige_soc_energie = None
            self.last_power_on_time = None
            self.last_power_off_time = None
            self._plan_start_time = None
            self._plan_start_meterstand = None
            self._sensor_storingen = 0

    def _send_push_notification(self, title: str, message: str):
        if hasattr(self, 'push_notification_entity') and self.push_notification_entity:
            try:
                service = self.push_notification_entity.replace("notify.", "notify/")
                self.call_service(
                    service,
                    title=title,
                    message=message,
                    data={"push": {"interruption-level": "time-sensitive"}},
                )
            except Exception as e:
                self.error(f"Fout bij verzenden pushmelding: {self._sanitize_error(e)}")

    def _check_kritieke_sensoren(
        self,
        include_vehicle_sensors: bool = True,
        include_status_sensor: bool = True,
        melden: bool = True,
    ) -> bool:
        kritieke_sensoren = [
            self.peblar_energie_entity,
            self.peblar_switch_entity,
            self.peblar_mode_entity,
        ]
        if include_status_sensor:
            kritieke_sensoren.append(self.peblar_status_entity)
        if include_vehicle_sensors:
            kritieke_sensoren.extend([self.soc_entity, self.bereik_entity])
        for sensor in kritieke_sensoren:
            state = self.get_state(sensor)
            if state is None or str(state).lower() in ["unavailable", "unknown"]:
                if melden:
                    self.error(f"Kritieke sensor onbeschikbaar: {sensor}")
                    self._send_push_notification("Peblar Fout", f"Kritieke sensor onbeschikbaar: {sensor}")
                else:
                    self.warning(f"Kritieke sensor onbeschikbaar: {sensor}")
                return False
        return True

    def _peblar_switch_changed(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if old == "on" and new != "on":
                self.last_power_off_time = datetime.datetime.now(tz.UTC)
                self.last_power_on_time = None
            elif old != "on" and new == "on":
                self.last_power_on_time = datetime.datetime.now(tz.UTC)
            if new == self._last_switch_command_state:
                commando_tijd = self._last_switch_command_time
                self._last_switch_command_state = None
                self._last_switch_command_time = None
                if (commando_tijd is not None and
                        (datetime.datetime.now(tz.UTC) - commando_tijd).total_seconds()
                        <= self.SWITCH_COMMAND_GRACE_SECONDS):
                    return
            if self._plan_status != "in_uitvoering" or str(
                self.get_state(self.PLAN_UITVOEREN_BOOLEAN) or ""
            ).lower() != "on":
                return
            expected_state = "on" if self._desired_power_on else "off"
            if new == expected_state:
                return
            self.log(
                f"Onverwachte handmatige wijziging van de Peblar-switch "
                f"({old} -> {new}); uitvoering wordt gepauzeerd."
            )
            self._set_execution_boolean(False)
            if new == "on":
                self._set_desired_state("Pure solar", False, 0.0)
            else:
                self._desired_power_on = False
                self._desired_kw = 0.0
                self._cancel_pending_control()
            self._plan_start_time = None
            self._plan_start_meterstand = None
            self._set_plan_status(
                "gepauzeerd",
                "De Peblar-switch is handmatig gewijzigd; controleer de laadpaal en hervat bewust.",
            )

    def _set_state_respecting_minimum_off(self, mode: str, power_on: bool, power_kw: float):
        if not power_on:
            self._cancel_minimum_off_timer()
            self._set_desired_state(mode, False, 0.0)
            return
        if self.get_state(self.peblar_switch_entity) != "on" and self.last_power_off_time is not None:
            elapsed = (datetime.datetime.now(tz.UTC) - self.last_power_off_time).total_seconds() / 60.0
            remaining = self.MIN_OFF_MINUTES - elapsed
            if remaining > 0:
                if self._minimum_off_timer is None:
                    self._minimum_off_timer = self.run_in(
                        self._minimum_off_elapsed_callback,
                        remaining * 60,
                    )
                self.log(f"ANTI-PENDEL: minimale uit-tijd, nog {remaining:.1f} min.")
                return
        self._cancel_minimum_off_timer()
        self._set_desired_state(mode, True, power_kw)

    def _minimum_off_elapsed_callback(self, kwargs):
        with self._lock:
            self._minimum_off_timer = None
            self.voer_schakeling_uit()

    def _cancel_minimum_off_timer(self):
        timer = getattr(self, "_minimum_off_timer", None)
        if timer is not None:
            self.cancel_timer(timer)
            self._minimum_off_timer = None

    def _soc_update_is_plausible(self, soc: float, meterstand: float) -> bool:
        if self.vorige_soc is None or self.vorige_soc_energie is None:
            self.vorige_soc = soc
            self.vorige_soc_energie = meterstand
            return True
        soc_delta = soc - self.vorige_soc
        if abs(soc_delta) <= self.TOLERANCE:
            return True
        energy_delta = max(0.0, meterstand - self.vorige_soc_energie)
        expected_soc_delta = (
            energy_delta * self.LAADRENDEMENT / self.ACCU_CAP_KWH * 100.0
        )
        if abs(soc_delta - expected_soc_delta) > self.MAX_SOC_JUMP_PER_QUARTER:
            self._log_bij_verandering(
                "soc_telemetry",
                f"SoC-update ({soc_delta:+.1f}%) past niet bij Peblar-meter "
                f"({expected_soc_delta:+.1f}% verwacht); FordPass-waarde tijdelijk genegeerd.",
            )
            return False
        self.vorige_soc = soc
        self.vorige_soc_energie = meterstand
        self._log_bij_verandering("soc_telemetry", None)
        return True


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
            self._save_persistent_data()
        self._schedule_kwartier_timer()

    def _plan_afwijking_kwh(self, huidige_energie: float, nu: datetime.datetime) -> Optional[float]:
        if self._plan_start_time is None or self._plan_start_meterstand is None:
            return None
        verwacht = 0.0
        matrix_per_tijd = {item["tijd"]: item for item in self.matrix_kwartieren}
        for tijd, gepland in self.plan.items():
            slot = matrix_per_tijd.get(tijd)
            if slot is None:
                continue
            duur_seconden = slot["duration_hours"] * 3600.0
            if duur_seconden <= 0:
                continue
            if tijd + datetime.timedelta(seconds=duur_seconden) <= self._plan_start_time:
                continue
            start = max(tijd, self._plan_start_time)
            verstreken = max(0.0, min(duur_seconden, (nu - start).total_seconds()))
            gepland_kwh = gepland.get("zon_kwh", 0.0) + gepland.get("net_kwh", 0.0)
            verwacht += gepland_kwh * verstreken / duur_seconden
        werkelijk = max(0.0, huidige_energie - self._plan_start_meterstand)
        return werkelijk - verwacht

    def _auto_status_changed(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self.planning_geannuleerd:
                return
            klasse = self._status_klasse(new)
            if klasse == "niet_beschikbaar":
                self._log_bij_verandering(
                    "peblar_status",
                    f"Peblar-status tijdelijk niet beschikbaar ('{new}'); "
                    "sessie blijft behouden tot statusherstel.",
                )
                return
            if klasse == "storing":
                self._log_bij_verandering(
                    "peblar_status", f"Peblar meldt status '{new}'; sessie blijft behouden.")
                if str(new).lower() in ("error", "fault"):
                    self._send_push_notification("Laadpaal Storing", f"Peblar-status: {new}")
                return
            if klasse == "onbekend":
                self._log_bij_verandering(
                    "peblar_status",
                    f"Onbekende Peblar-status '{new}'; sessie blijft behouden. "
                    "Controleer status_verbonden/status_losgekoppeld in de configuratie.",
                )
                return
            self._log_bij_verandering("peblar_status", None)
            if klasse == "losgekoppeld":
                self.log(f"Auto losgekoppeld (status: {new}). Harde stop.")
                self._send_push_notification("Auto Losgekoppeld", f"Auto is losgekoppeld. Status: {new}")
                self._set_execution_boolean(False)
                self._set_desired_state("Pure solar", False, 0.0)
                self._reset_laadtracking()
                self._clear_plan()
                self._set_plan_status("opnieuw_berekenen", "Auto losgekoppeld; maak na opnieuw aansluiten een nieuw plan.")
                self._save_persistent_data()
            else:  # verbonden: 'charging' of 'suspended' (0 W is normaal tijdens een sessie)
                huidige_soc = self.get_sensor_float(self.soc_entity)
                if not self.sessie_actief:
                    self.sessie_actief = True
                    self.initiele_energie = None
                    self.vorige_soc = huidige_soc
                    self.vorige_soc_energie = None
                    self.planning_geannuleerd = False
                    self.log(f"Auto verbonden (status: {new}). Nieuwe sessie gestart, SoC={huidige_soc}%")

    def _sanitize_error(self, error: Exception) -> str:
        error_str = str(error)
        error_str = re.sub(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b",
            "[REDACTED_IP]",
            error_str,
        )
        return re.sub(
            r"(?i)\b(password|pwd|secret|token)\b(\s*[:=]\s*)([^\s,;]+)",
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            error_str,
        )

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

    def get_sensor_float(self, entity_id: str, default: Optional[float] = None) -> Optional[float]:
        state = self.get_state(entity_id)
        if state is None or str(state).lower() in ["unavailable", "unknown"]:
            return default
        try:
            return max(0.0, float(state))
        except (ValueError, TypeError):
            return default

    def _status_lijst(self, sleutel: str, standaard) -> tuple:
        waarde = self.args.get(sleutel)
        if waarde is None:
            return tuple(standaard)
        if isinstance(waarde, str):
            waarde = waarde.split(",")
        return tuple(str(item).strip().lower() for item in waarde if str(item).strip())

    def _status_klasse(self, status) -> str:
        """Deelt een Peblar-status in: verbonden / losgekoppeld / storing / niet_beschikbaar / onbekend."""
        tekst = str(status).strip().lower() if status is not None else ""
        if tekst in ("", "none", "unavailable", "unknown"):
            return "niet_beschikbaar"
        if tekst in self.STATUS_VERBONDEN:
            return "verbonden"
        if tekst in self.STATUS_LOSGEKOPPELD:
            return "losgekoppeld"
        if tekst in self.STATUS_STORING:
            return "storing"
        return "onbekend"

    def _is_auto_verbonden(self) -> bool:
        return self._status_klasse(self.get_state(self.peblar_status_entity)) == "verbonden"

    def _ampere_naar_kw(self, ampere: float) -> float:
        if self.FASEN == 1:
            return (ampere * self.SPANNING_V) / 1000
        elif self.FASEN == 3:
            return (ampere * self.SPANNING_V * 3) / 1000
        else:
            self.error(f"Ongeldig aantal fasen: {self.FASEN}.")
            return self.MAX_PAAL_KW

    def _kw_naar_ampere(self, vermogen_kw: float) -> float:
        if self.FASEN not in (1, 3) or self.SPANNING_V <= 0:
            self.error(f"Ongeldige laadconfiguratie: {self.FASEN} fasen, {self.SPANNING_V} V.")
            return self.MAX_AMPERE
        return (vermogen_kw * 1000.0) / (self.SPANNING_V * self.FASEN)

    def _zonoverschot_bruikbaar(self, vermogen_kw: float, energie_kwh: float) -> bool:
        return (
            energie_kwh >= self.TOLERANCE
            and vermogen_kw >= self.MIN_LAADVERMOGEN_ZON_KW
        )

    _SNAPSHOT_ATTRS = (
        "plan", "matrix_kwartieren", "last_plan_calculation", "_plan_start_time",
        "_plan_start_meterstand", "doel_soc", "sessie_doel_kwh", "cumulatief_geladen_kwh",
        "initiele_energie", "_last_progress_kwh", "sessie_actief", "vorige_soc", "vorige_soc_energie",
    )

    def _plan_snapshot(self) -> Dict[str, Any]:
        return {naam: getattr(self, naam) for naam in self._SNAPSHOT_ATTRS}

    def _plan_herstellen(self, snapshot: Dict[str, Any]):
        for naam, waarde in snapshot.items():
            setattr(self, naam, waarde)

    def _effectieve_soc(self, soc: float, meterstand: float) -> float:
        """Beste SoC-schatting. FordPass blijft leidend; is de waarde sinds de laatste
        wijziging niet bijgewerkt terwijl er wel is geladen, dan wordt de SoC geschat
        uit de Peblar-meter (verouderde FordPass-waarde)."""
        if self.vorige_soc is None or self.vorige_soc_energie is None:
            return soc
        if abs(soc - self.vorige_soc) > self.TOLERANCE:
            return soc
        geladen_kwh = max(0.0, meterstand - self.vorige_soc_energie)
        schatting = min(100.0, soc + geladen_kwh * self.LAADRENDEMENT / self.ACCU_CAP_KWH * 100.0)
        if schatting - soc >= 0.5:
            self._log_bij_verandering(
                "soc_schatting",
                f"FordPass-SoC ({soc:.0f}%) lijkt verouderd; geschat op {schatting:.1f}% via de Peblar-meter.")
        else:
            self._log_bij_verandering("soc_schatting", None)
        return schatting

    def _registreer_soc(self, soc: float, meterstand: float):
        """Onthoudt de laatst gemelde FordPass-SoC met de bijbehorende meterstand
        (alleen bij een gewijzigde waarde, zodat een verouderde waarde herkenbaar blijft)."""
        if (self.vorige_soc is None or self.vorige_soc_energie is None
                or abs(soc - self.vorige_soc) > self.TOLERANCE):
            self.vorige_soc = soc
            self.vorige_soc_energie = meterstand

    def _parse_vertrektijd(self, tijd_str: str) -> datetime.datetime:
        attrs = self.get_state(self.vertrektijd_entity, attribute="all")
        attributes = (attrs or {}).get("attributes", {}) or {}
        has_date = attributes.get("has_date", False)
        has_time = attributes.get("has_time", False)
        try:
            if has_date and has_time:
                try:
                    lokaal = datetime.datetime.strptime(tijd_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._local_tz)
                except ValueError:
                    lokaal = datetime.datetime.fromisoformat(tijd_str).replace(tzinfo=self._local_tz)
            elif has_time:
                lokale_nu = datetime.datetime.now(self._local_tz)
                tijd_obj = datetime.datetime.strptime(tijd_str, "%H:%M:%S").time()
                lokaal = lokale_nu.replace(hour=tijd_obj.hour, minute=tijd_obj.minute, second=0, microsecond=0)
                if lokaal <= lokale_nu:
                    lokaal += datetime.timedelta(days=1)
            else:
                raise PlanningAbort("Vertrektijd heeft geen datum/tijd!")
        except ValueError:
            raise PlanningAbort(f"Kan vertrektijd niet parsen: {tijd_str}")
        return lokaal.astimezone(tz.UTC)

    def _bouw_matrix(self, forecast, nu: datetime.datetime, vertrektijd: datetime.datetime):
        matrix = []
        ontbrekend = 0
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

            fc_slot = forecast.get(slot_start)
            if fc_slot is None:
                ontbrekend += 1
                fc_slot = {
                    "pv_kw": 0.0,
                    "verbruik_kw": self.FALLBACK_VERBRUIK_KW,
                    "epex_prijs": 0.0,
                    "net_prijs": self.FALLBACK_NET_PRIJS,
                    "prijs_bron": "fallback",
                }

            verbruik_kw = fc_slot["verbruik_kw"]
            pv_kw = fc_slot["pv_kw"] if duration_hours > 0 else 0.0
            base_import_kw = max(0.0, verbruik_kw - pv_kw)
            solar_surplus_kw = max(0.0, pv_kw - verbruik_kw)
            zon_laad_kw = min(self.MAX_PAAL_KW, solar_surplus_kw)
            laadbare_zon_kwh = zon_laad_kw * duration_hours

            if not self._zonoverschot_bruikbaar(zon_laad_kw, laadbare_zon_kwh):
                laadbare_zon_kwh = 0.0
                zon_laad_kw = 0.0

            rest_paal_kw = self.MAX_PAAL_KW - zon_laad_kw
            max_net_kw = min(rest_paal_kw, max(0.0, self.MAX_GROEP_KW - base_import_kw))
            max_net_kwh_kwartier = max_net_kw * duration_hours

            matrix.append({
                "tijd": slot_start,
                "duration_hours": duration_hours,
                "laadbare_zon_kwh": laadbare_zon_kwh,
                "max_net_kwh": max_net_kwh_kwartier,
                "net_prijs": fc_slot["net_prijs"],
                "epex_prijs": fc_slot["epex_prijs"],
                "pv_kw": fc_slot["pv_kw"],
                "verbruik_kw": verbruik_kw,
                "prijs_bron": fc_slot["prijs_bron"],
            })
            slot_start = slot_end
        return matrix, ontbrekend

    def bereken_laadplan(self, concept: bool = True) -> bool:
        """Berekent het laadplan; True bij succes.

        concept=True : nieuw conceptplan (knop). Bij mislukken wordt de paal veilig gestopt
                       en de sessie gereset.
        concept=False: correctie tijdens uitvoering. Bij mislukken (bv. FordPass of forecast
                       onbeschikbaar) blijft het bestaande plan ongewijzigd actief.
        """
        with self._lock:
            if self.planning_geannuleerd:
                self.log("Planning is geannuleerd, geen herberekening.")
                return False
            snapshot = self._plan_snapshot()
            try:
                self._bereken_laadplan_uitvoeren(concept)
                self._log_bij_verandering("plancorrectie", None)
                return True
            except PlanningAbort as abort:
                self._plan_afgebroken(abort, concept, snapshot)
            except Exception as e:
                self.error(f"Fout in bereken_laadplan: {self._sanitize_error(e)}")
                self._plan_afgebroken(PlanningAbort("Interne fout bij plannen."), concept, snapshot, gelogd=True)
            return False

    def _plan_afgebroken(self, abort: PlanningAbort, concept: bool, snapshot: Dict[str, Any], gelogd: bool = False):
        if not concept:
            self._plan_herstellen(snapshot)
            self._log_bij_verandering(
                "plancorrectie",
                f"Plancorrectie niet mogelijk ({abort.reden}); bestaand plan blijft actief.")
            return
        if not gelogd:
            if abort.fout:
                self.error(abort.reden)
            else:
                self.log(abort.reden)
        self._wacht_op_forecast = abort.wacht_forecast
        if abort.afgekoppeld:
            self._set_execution_boolean(False)
            self._set_desired_state("Pure solar", False, 0.0)
            self._reset_laadtracking()
            self._clear_plan()
            self._set_plan_status("opnieuw_berekenen", "Auto losgekoppeld; maak na opnieuw aansluiten een nieuw plan.")
        else:
            self._clear_plan()
            self._set_desired_state("Pure solar", False, 0.0)
            self._reset_laadtracking()
        self._save_persistent_data()

    def _bereken_laadplan_uitvoeren(self, concept: bool):
        if not self._check_kritieke_sensoren(melden=concept):
            raise PlanningAbort("Kritieke sensor onbeschikbaar.")
        self._clear_plan()
        tijd_str = self.get_state(self.vertrektijd_entity)
        if tijd_str is None:
            raise PlanningAbort("Vertrektijd is None! Stoppen.")
        vertrektijd = self._parse_vertrektijd(tijd_str)

        nu = datetime.datetime.now(tz.UTC)
        if vertrektijd <= nu:
            raise PlanningAbort("Vertrektijd in verleden. Standby.", fout=False)
        if not self._is_auto_verbonden():
            raise PlanningAbort("Geen auto verbonden. Standby.", fout=False, afgekoppeld=True)

        # Doel-SoC en behoefte (FordPass is leidend; bij verouderde SoC schatten via de Peblar-meter)
        gewenste_km = self.get_sensor_float(self.gewenste_km_entity)
        huidig_bereik = self.get_sensor_float(self.bereik_entity)
        huidige_soc = self.get_sensor_float(self.soc_entity)
        if None in [gewenste_km, huidig_bereik, huidige_soc]:
            raise PlanningAbort("Kritieke sensoren ontbreken!")
        huidige_energie = self.get_sensor_float(self.peblar_energie_entity)
        if huidige_energie is None:
            raise PlanningAbort("Energie sensor ongeldig!")

        effectieve_soc = self._effectieve_soc(huidige_soc, huidige_energie)
        km_per_procent = (huidig_bereik / huidige_soc if huidige_soc > 0 and huidig_bereik > 0 else self.FALLBACK_KM_PER_PERCENT)
        doel_soc = max(self.MIN_SOC_SAFETY, min(100.0, (gewenste_km / km_per_procent) + self.VEILIGHEIDSMARGE_PERCENT))
        netto_behoefte_kwh = max(0.0, ((doel_soc - effectieve_soc) / 100.0) * self.ACCU_CAP_KWH)
        laad_behoefte_kwh = netto_behoefte_kwh / self.LAADRENDEMENT
        self.log(f"SoC: {effectieve_soc:.1f}% -> {doel_soc:.1f}% (+{self.VEILIGHEIDSMARGE_PERCENT}% marge). "
                 f"Netladen behoefte: {netto_behoefte_kwh:.2f} kWh ({laad_behoefte_kwh:.2f} kWh uit het net)")

        forecast = self._lees_forecast()
        if forecast is None:
            raise PlanningAbort("Forecast niet beschikbaar of verouderd.", fout=False, wacht_forecast=True)
        self._wacht_op_forecast = False

        self.matrix_kwartieren, ontbrekend = self._bouw_matrix(forecast, nu, vertrektijd)

        if ontbrekend > 0:
            self._log_bij_verandering("forecast_ontbreekt",
                                      f"{ontbrekend} kwartier(en) niet in forecast, fallbackwaarden gebruikt.")
        else:
            self._log_bij_verandering("forecast_ontbreekt", None)

        fallback_kwartieren = [u for u in self.matrix_kwartieren if u["prijs_bron"] == "fallback"]
        if fallback_kwartieren:
            eerste_fallback = fallback_kwartieren[0]["tijd"].astimezone(self._local_tz).strftime("%d-%m %H:%M")
            self._log_bij_verandering(
                "fallback_prijzen",
                f"{len(fallback_kwartieren)} van {len(self.matrix_kwartieren)} kwartieren tot vertrektijd gebruiken "
                f"een fallbackprijs (eerste: {eerste_fallback}). De planning is daar minder betrouwbaar.")
        else:
            self._log_bij_verandering("fallback_prijzen", None)

        # Plan zonladen en netladen
        if laad_behoefte_kwh > self.TOLERANCE:
            totale_capaciteit = sum(u["laadbare_zon_kwh"] + u["max_net_kwh"] for u in self.matrix_kwartieren)
            if totale_capaciteit < laad_behoefte_kwh - self.TOLERANCE:
                raise PlanningAbort(f"Onvoldoende capaciteit: {totale_capaciteit:.2f} kWh beschikbaar.")
            if not self._bereken_geintegreerd_plan(laad_behoefte_kwh):
                self._plan_zo_veel_mogelijk(laad_behoefte_kwh)
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

        # Plan is gelukt: pas nu de sessiebaseline aan (SoC-gebaseerde behoefte vanaf nu)
        self._cancel_progress_update_timer()
        self.initiele_energie = max(0.0, huidige_energie)
        self.cumulatief_geladen_kwh = 0.0
        self._last_progress_kwh = 0.0
        self.sessie_actief = True
        self.sessie_doel_kwh = laad_behoefte_kwh
        self.doel_soc = doel_soc
        self._registreer_soc(huidige_soc, huidige_energie)
        self.log(f"Sessiebaseline bijgewerkt: initiele energie = {self.initiele_energie:.2f} kWh, SoC = {huidige_soc}%")

        self.last_plan_calculation = nu
        if concept:
            self._set_plan_status("concept", "Wacht op goedkeuring via de uitvoerschakelaar.")
        else:
            self._plan_start_time = nu
            self._plan_start_meterstand = huidige_energie
            self._set_plan_status("in_uitvoering")
        self._save_persistent_data()
        self._update_graph_data()

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
                    window_cost < min_window_cost - 1e-9):
                    min_window_cost = window_cost
                    best_start_idx = start_idx
                    best_end_idx = start_idx + idx

                if idx > 0:
                    self.log(f"Window {start_idx}: {gevuld_kwh:.2f} kWh, €{window_cost:.2f}", level="DEBUG")

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

            self.log(f"Best window: {best_start_idx}-{best_end_idx}, €{min_window_cost:.2f}", level="DEBUG")
            return True

    def _plan_zo_veel_mogelijk(self, laad_behoefte_kwh: float):
        with self._lock:
            self.plan = {}
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
                if (self._plan_status != "in_uitvoering" or
                    str(self.get_state(self.PLAN_UITVOEREN_BOOLEAN)).lower() != "on"):
                    self._set_desired_state("Pure solar", False, 0.0)
                    return

                nu = datetime.datetime.now(tz.UTC)
                huidige_soc = self.get_sensor_float(self.soc_entity)
                huidige_energie = self.get_sensor_float(self.peblar_energie_entity)

                # Peblar-sensoren: korte storingen tolereren, langdurige storing stopt de sessie
                if huidige_energie is None or not self._check_kritieke_sensoren(
                    include_vehicle_sensors=False,
                    include_status_sensor=False,
                    melden=False,
                ):
                    self._sensor_storingen += 1
                    if self._sensor_storingen < self.MAX_SENSOR_STORING_KWARTIEREN:
                        self._log_bij_verandering(
                            "peblar_sensoren",
                            "Peblar-sensor(en) tijdelijk onbeschikbaar; laadpaal blijft in de huidige stand "
                            "en de schakeling wacht op herstel.")
                        if self._sensor_storingen == 1:
                            self._send_push_notification(
                                "Sensorstoring", "Peblar-sensor(en) onbeschikbaar; schakeling wacht op herstel.")
                        return
                    self.error("Peblar-sensoren langdurig onbeschikbaar!")
                    self._send_push_notification("Sensorstoring", "Peblar-sensor(en) langdurig onbeschikbaar. Sessie gestopt.")
                    self._set_execution_boolean(False)
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._set_plan_status("fout", "Peblar-sensor ontbreekt; uitvoering gestopt.")
                    self._save_persistent_data()
                    return
                self._sensor_storingen = 0
                self._log_bij_verandering("peblar_sensoren", None)

                auto_status = self.get_state(self.peblar_status_entity)
                status_klasse = self._status_klasse(auto_status)
                if status_klasse == "losgekoppeld":
                    self.log("Auto losgekoppeld. Harde stop.")
                    self._set_execution_boolean(False)
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._reset_laadtracking()
                    self._clear_plan()
                    self._set_plan_status(
                        "opnieuw_berekenen",
                        "Auto losgekoppeld; maak na opnieuw aansluiten een nieuw plan.",
                    )
                    self._save_persistent_data()
                    return
                if status_klasse != "verbonden":
                    self._log_bij_verandering(
                        "peblar_status",
                        f"Peblar-status '{auto_status}' is niet bruikbaar voor schakeling; "
                        "sessie blijft behouden en schakeling wacht op herstel.",
                    )
                    return
                self._log_bij_verandering("peblar_status", None)

                if huidige_soc is not None and not (0 <= huidige_soc <= 100):
                    self._log_bij_verandering("soc_telemetry", f"Ongeldige FordPass SoC-waarde: {huidige_soc}%.")
                    huidige_soc = None
                elif huidige_soc is not None and not self._soc_update_is_plausible(huidige_soc, huidige_energie):
                    huidige_soc = None

                huidig_kwartier = nu.replace(second=0, microsecond=0, minute=(nu.minute // 15) * 15, tzinfo=tz.UTC)

                afwijking_kwh = self._plan_afwijking_kwh(huidige_energie, nu)
                plan_ontbreekt = not self.matrix_kwartieren
                grote_afwijking = (afwijking_kwh is not None and
                                   abs(afwijking_kwh) >= self.PLAN_CORRECTIE_TOLERANTIE_KWH)
                if plan_ontbreekt:
                    self._set_execution_boolean(False)
                    self._set_desired_state("Pure solar", False, 0.0)
                    self._set_plan_status("concept_verlopen", "Bereken en keur een nieuw plan goed.")
                    return
                if grote_afwijking:
                    self.log(f"Laadenergie wijkt {afwijking_kwh:+.2f} kWh af van plan; planning corrigeren.")
                    if huidige_soc is None:
                        self._log_bij_verandering(
                            "plancorrectie",
                            "Plancorrectie uitgesteld: FordPass-SoC niet betrouwbaar beschikbaar; "
                            "bestaand plan blijft actief.")
                    elif self.bereken_laadplan(concept=False):
                        huidig_kwartier = nu.replace(second=0, microsecond=0, minute=(nu.minute // 15) * 15, tzinfo=tz.UTC)
                    # Mislukt de correctie, dan herstelt bereken_laadplan de vorige staat en loopt het plan door.

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
                        self._set_execution_boolean(False)
                        self._set_desired_state("Pure solar", False, 0.0)
                        self._reset_laadtracking()
                        self._set_plan_status("voltooid")
                        self._save_persistent_data()
                        return

                switch_state = self.get_state(self.peblar_switch_entity)
                if switch_state in [None, "unavailable", "unknown"]:
                    self.error("Switch niet beschikbaar!")
                    self._set_execution_boolean(False)
                    self._set_plan_status("fout", "Laadpaalschakelaar is niet beschikbaar.")
                    return

                current_power_on = (switch_state == "on")

                # Haal plan voor huidige kwartier
                plan = self.plan.get(huidig_kwartier, {"zon_kwh": 0.0, "net_kwh": 0.0})
                zon_kwh = plan.get("zon_kwh", 0.0)
                net_kwh = plan.get("net_kwh", 0.0)

                # Track power on/off tijd
                if current_power_on and self.last_power_on_time is None:
                    self.last_power_on_time = nu
                elif not current_power_on:
                    self.last_power_on_time = None

                # NOOD: SoC te laag (verouderde FordPass-waarde wordt via de meter bijgeschat)
                soc_voor_nood = (self._effectieve_soc(huidige_soc, huidige_energie)
                                 if huidige_soc is not None else None)
                if soc_voor_nood is not None and soc_voor_nood < self.MIN_SOC_SAFETY:
                    self.log(f"NOOD: SoC {soc_voor_nood:.1f}% < {self.MIN_SOC_SAFETY}%. Maximaal laden.")
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
                    self._set_state_respecting_minimum_off("Default", True, kw)
                # Zonladen
                elif zon_kwh > self.TOLERANCE:
                    duration = next((u["duration_hours"] for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), self.QUARTER_HOURS)
                    kw = zon_kwh / duration if duration > 0 else 0.0
                    self._set_state_respecting_minimum_off("Pure solar", True, kw)
                # Zonladen altijd mogelijk
                else:
                    actueel = next((u for u in self.matrix_kwartieren if u["tijd"] == huidig_kwartier), None)
                    if actueel and actueel["laadbare_zon_kwh"] > self.TOLERANCE:
                        duration = actueel.get("duration_hours", self.QUARTER_HOURS)
                        kw = actueel["laadbare_zon_kwh"] / duration if duration > 0 else 0.0
                        self._set_state_respecting_minimum_off("Pure solar", True, kw)
                    else:
                        if current_power_on and self.last_power_on_time is not None:
                            run_duration = (nu - self.last_power_on_time).total_seconds() / 60.0
                            if run_duration < self.MIN_RUN_MINUTES:
                                self.log(
                                    f"ANTI-PENDEL: minimale draaitijd, nog "
                                    f"{self.MIN_RUN_MINUTES - run_duration:.1f} min."
                                )
                                return
                        self._set_desired_state("Pure solar", False, 0.0)

            except Exception as e:
                self.error(f"Fout in voer_schakeling_uit: {self._sanitize_error(e)}")
                self._set_execution_boolean(False)
                self._set_desired_state("Pure solar", False, 0.0)
                self._reset_laadtracking()
                self._set_plan_status("fout", "Uitvoering gestopt door een interne fout.")
                self._save_persistent_data()

    def _update_graph_data(self):
        try:
            nu = datetime.datetime.now(tz.UTC)
            generated_at = nu.isoformat()
            if not self.matrix_kwartieren:
                attributes = {
                    "friendly_name": "Peblar Horizon Planner Graph Data",
                    "icon": "mdi:chart-line",
                    "device_class": "timestamp",
                    "generated_at": generated_at,
                    "slot_count": 0,
                    "data_entities": {},
                    "metadata": {"plan_status": self._plan_status},
                    "tijden": [],
                    "epex_tarieven": [],
                    "totaal_tarieven": [],
                    "zon_opwek": [],
                    "huis_verbruik": [],
                    "laadplanning_zon": [],
                    "laadplanning_net": [],
                    "prijs_bron": [],
                }
                self._markeer_graph_paginas_stale(generated_at)
                self.set_state(self.GRAPH_DATA_ENTITY, state=generated_at, attributes=attributes)
                return

            tijden = []
            epex_tarieven = []
            totaal_tarieven = []
            zon_opwek = []
            huis_verbruik = []
            prijs_bron = []
            laadplanning_zon = []
            laadplanning_net = []
            for u in self.matrix_kwartieren:
                tijden.append(u["tijd"].strftime("%Y-%m-%d %H:%M"))
                epex_tarieven.append(round(u["epex_prijs"], 4))
                totaal_tarieven.append(round(u["net_prijs"], 4))
                zon_opwek.append(round(u["laadbare_zon_kwh"], 2))
                huis_verbruik.append(round(u["verbruik_kw"], 2))
                prijs_bron.append(u["prijs_bron"])
                plan_entry = self.plan.get(u["tijd"], {"zon_kwh": 0.0, "net_kwh": 0.0})
                laadplanning_zon.append(round(plan_entry.get("zon_kwh", 0.0), 2))
                laadplanning_net.append(round(plan_entry.get("net_kwh", 0.0), 2))

            graph_topics = {
                "tijden": {},
                "prijzen": {
                    "epex_tarieven": epex_tarieven,
                    "totaal_tarieven": totaal_tarieven,
                    "prijs_bron": prijs_bron,
                },
                "energie": {
                    "zon_opwek": zon_opwek,
                    "huis_verbruik": huis_verbruik,
                },
                "planning": {
                    "laadplanning_zon": laadplanning_zon,
                    "laadplanning_net": laadplanning_net,
                },
            }
            data_entities = {
                topic: self._publish_graph_topic(
                    topic,
                    tijden,
                    fields,
                    generated_at,
                    include_tijden=(topic == "tijden"),
                )
                for topic, fields in graph_topics.items()
            }
            metadata = {
                "energiebelasting": self._tarief_meta.get("energiebelasting"),
                "leverancierskosten": self._tarief_meta.get("leverancierskosten"),
                "btw": self._tarief_meta.get("btw"),
                "laadrendement": self.LAADRENDEMENT,
                "forecast_status": self._forecast_status,
                "plan_status": self._plan_status,
                "fallback_prijs_kwartieren": sum(
                    1 for u in self.matrix_kwartieren if u["prijs_bron"] == "fallback"
                ),
            }
            legacy_data = {
                "tijden": tijden,
                "epex_tarieven": epex_tarieven,
                "totaal_tarieven": totaal_tarieven,
                "zon_opwek": zon_opwek,
                "huis_verbruik": huis_verbruik,
                "laadplanning_zon": laadplanning_zon,
                "laadplanning_net": laadplanning_net,
                "prijs_bron": prijs_bron,
            }
            attributes = {
                "friendly_name": "Peblar Horizon Planner Graph Data",
                "icon": "mdi:chart-line",
                "device_class": "timestamp",
                "generated_at": generated_at,
                "slot_count": len(self.matrix_kwartieren),
                "data_entities": data_entities,
                "data_schema_version": 2,
                "data_alignment": "Concatenate pages in page order; align topic values by start_index and slot_count.",
                "legacy_arrays_available": False,
                "metadata": metadata,
            }
            legacy_attributes = {**attributes, **legacy_data}
            if len(json.dumps(legacy_attributes, separators=(",", ":")).encode("utf-8")) <= self.GRAPH_ATTRIBUTE_MAX_BYTES:
                attributes["legacy_arrays_available"] = True
                attributes.update(legacy_data)
            self.set_state(self.GRAPH_DATA_ENTITY, state=generated_at, attributes=attributes)
        except Exception as e:
            self.error(f"Fout bij updaten grafiek data: {self._sanitize_error(e)}")

    def _publish_graph_topic(
        self,
        topic: str,
        tijden: list,
        fields: Dict[str, list],
        generated_at: str,
        include_tijden: bool = False,
    ) -> list:
        pages = []
        start_index = 0
        page_data = {**({"tijden": []} if include_tijden else {}), **{field: [] for field in fields}}

        def slot_count(data):
            if include_tijden:
                return len(data["tijden"])
            return len(next(iter(data.values())))

        def attributes_for(data, page_number, start, page_count=9999):
            return {
                "friendly_name": f"Peblar Horizon Planner {topic}",
                "generated_at": generated_at,
                "topic": topic,
                "page": page_number,
                "page_count": page_count,
                "start_index": start,
                "slot_count": slot_count(data),
                **data,
            }

        for index, tijd in enumerate(tijden):
            if include_tijden:
                page_data["tijden"].append(tijd)
            for field, values in fields.items():
                page_data[field].append(values[index])
            size = len(json.dumps(
                attributes_for(page_data, len(pages) + 1, start_index),
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8"))
            if size > self.GRAPH_ATTRIBUTE_MAX_BYTES:
                for values in page_data.values():
                    values.pop()
                if slot_count(page_data) == 0:
                    raise ValueError(f"Een grafiekwaarde voor '{topic}' overschrijdt de attribuutlimiet.")
                pages.append((start_index, page_data))
                start_index = index
                page_data = {
                    **({"tijden": [tijd]} if include_tijden else {}),
                    **{field: [values[index]] for field, values in fields.items()},
                }

        if slot_count(page_data):
            pages.append((start_index, page_data))

        entity_ids = []
        for page_number, (start, data) in enumerate(pages, start=1):
            entity_id = f"{self.GRAPH_DATA_ENTITY}_{topic}_{page_number}"
            self.set_state(
                entity_id,
                state=generated_at,
                attributes=attributes_for(data, page_number, start, len(pages)),
            )
            entity_ids.append(entity_id)
        self._markeer_graph_paginas_stale(generated_at, topic=topic, vanaf=len(entity_ids) + 1)
        self._graph_page_counts[topic] = len(entity_ids)
        return entity_ids

    def _markeer_graph_paginas_stale(self, generated_at: str, topic: Optional[str] = None, vanaf: int = 1):
        """Pagina-entiteiten uit een eerdere berekening kunnen niet worden verwijderd; markeer ze als leeg."""
        topics = [topic] if topic is not None else list(self._graph_page_counts.keys())
        for naam in topics:
            for nummer in range(vanaf, self._graph_page_counts.get(naam, 0) + 1):
                self.set_state(
                    f"{self.GRAPH_DATA_ENTITY}_{naam}_{nummer}",
                    state="leeg",
                    attributes={
                        "friendly_name": f"Peblar Horizon Planner {naam}",
                        "generated_at": generated_at,
                        "topic": naam,
                        "page": nummer,
                        "slot_count": 0,
                        "stale": True,
                    },
                )
            if topic is None:
                self._graph_page_counts[naam] = 0

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

            # Keep the charger limit open in solar mode; a prior Default setpoint
            # must not silently cap solar charging.
            if self._desired_mode in ("Default", "Pure solar"):
                laadlimiet_kw = (
                    self._desired_kw if self._desired_mode == "Default" else self.MAX_PAAL_KW
                )
                if not self._set_laadvermogen(laadlimiet_kw):
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
        if current_switch != "off":
            self._last_switch_command_state = "off"
            self._last_switch_command_time = datetime.datetime.now(tz.UTC)
            self.call_service("switch/turn_off", entity_id=self.peblar_switch_entity)
            self.last_switch_call = datetime.datetime.now(tz.UTC)
            self.last_power_off_time = self.last_switch_call
            self.last_power_on_time = None
            self.log("Peblar uitgeschakeld.")

    def _force_switch_on(self):
        current_switch = self.get_state(self.peblar_switch_entity)
        if current_switch != "on":
            self._last_switch_command_state = "on"
            self._last_switch_command_time = datetime.datetime.now(tz.UTC)
            self.call_service("switch/turn_on", entity_id=self.peblar_switch_entity)
            self.last_switch_call = datetime.datetime.now(tz.UTC)
            self.last_power_on_time = self.last_switch_call
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
