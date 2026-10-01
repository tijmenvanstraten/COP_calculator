import hassapi as hass
import datetime
import threading
import time
from typing import Dict, List, Optional, Any, Tuple, Set
from dateutil import tz


class EnergieCorePredictor(hass.Hass):
    """
    Zelfstandige voorspeller voor huisverbruik, Solcast-opbrengst en EPEX-prijzen.
    Bouwt elk kwartier de 'matrix_kwartieren' (72 uur vooruit) en schrijft die als lijst
    naar het attribuut 'forecast' van sensor.energie_core_forecast.

    Alle energiewaarden in de matrix zijn kWh per kwartier. Verbruik komt uit de
    cumulatieve tellers (Stand_einde - Stand_begin) via de statistics van de recorder.
    """

    # ===== CONFIGURATIE =====
    SCHEMA_VERSIE = 2
    QUARTER_MINUTES = 15
    QUARTER_HOURS = 0.25
    FALLBACK_EPEX_PRIJS = 0.20
    FALLBACK_VERBRUIK_KW = 0.11
    MIN_VERBRUIK_KW = 0.04
    EPEX_UNIT_MULTIPLIER = 1.0
    MAX_EPEX_FORECAST_HOURS = 72
    DEBUG_LOGGING = False

    # Belastingen (2026)
    ENERGIEBELASTING_PER_KWH = 0.15
    LEVERANCIERSKOSTEN_PER_KWH = 0.03
    BTW_PERCENTAGE = 21.0

    # EpexPredictor: True als de REST-sensor al belasting en btw bevat (via surcharge/taxPercent).
    # Zet dit op False en gebruik surcharge=0 en taxPercent=0 in de REST-URL om dubbel tellen te voorkomen.
    EPEX_PRIJS_IS_TOTAAL = False

    # Solcast: True = pv_estimate is gemiddeld vermogen in kW per periode van 30 minuten.
    # False = pv_estimate is kWh per periode van 30 minuten.
    SOLCAST_WAARDE_IS_KW = False

    # Verbruik uit cumulatieve kWh-tellers: verbruik = som(plus) - som(min), per kwartier
    MAX_REALISTISCH_VERBRUIK_KW = 30.0

    # Historie
    HISTORY_DAYS = 30
    VORIG_JAAR_MARGE_DAGEN = 15
    VORIG_JAAR_GEWICHT = 0.25
    HISTORY_REFRESH_HOURS = 12
    HISTORY_RETRY_MINUTES = 30
    HISTORY_MIN_DAGEN = 2
    HISTORY_START_DELAY_SECONDS = 20

    # Belasting van Home Assistant bij het ophalen van statistics
    STATS_CHUNK_DAGEN_5MIN = 10
    STATS_CHUNK_DAGEN_UUR = 60
    STATS_PAUZE_SECONDEN = 0.05
    STATS_TIMEOUT_SECONDEN = 60
    STATS_PROBE_MIN_RIJEN = 100

    # Timers
    KWARTIER_DELAY_SECONDS = 0
    DEBOUNCE_SECONDS = 10
    MIN_REBUILD_SECONDS = 30

    LOCAL_TZ_NAME = "Europe/Amsterdam"

    # ===== ENTITY CONFIGURATIE =====
    DEFAULT_ENTITIES = {
        "solcast_today_entity": "sensor.solcast_pv_forecast_forecast_today",
        "solcast_tomorrow_entity": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "epex_prijs_entity": "sensor.epex_price_prediction",
        "forecast_entity": "sensor.energie_core_forecast",
    }
    # Cumulatieve kWh-tellers (state_class total_increasing). PAS DEZE AAN in apps.yaml.
    DEFAULT_PLUS_ENTITIES = [
        "sensor.p1_energie_import_tarief_1",
        "sensor.p1_energie_import_tarief_2",
        "sensor.zonnepanelen_energie_totaal",
    ]
    DEFAULT_MIN_ENTITIES = [
        "sensor.p1_energie_export_tarief_1",
        "sensor.p1_energie_export_tarief_2",
        "sensor.peblar_ev_charger_levenslange_energie",
    ]
    DEFAULT_SOLCAST_EXTRA_ENTITIES: List[str] = []

    def initialize(self):
        self.log("Energie Core Predictor v3 opgestart.")
        self._lock = threading.RLock()
        self._load_configuration()
        self._local_tz = tz.gettz(self.LOCAL_TZ_NAME)

        self.profiel_recent: Dict[Tuple, float] = {}
        self.profiel_vorig_jaar: Dict[Tuple, float] = {}
        self.historie_geladen_op: Optional[datetime.datetime] = None
        self._history_next_try: Optional[datetime.datetime] = None
        self._historie_bezig = False
        self._historie_waarschuwingen: List[str] = []
        self._historie_bronnen: Dict[str, str] = {}
        self._laad_problemen: Set[str] = set()
        self._gelogde_waarschuwingen: Set[str] = set()
        self.prijs_dict: Dict[datetime.datetime, float] = {}
        self.forecast_dict: Dict[datetime.datetime, float] = {}
        self.matrix_kwartieren: List[Dict[str, Any]] = []
        self.last_build: Optional[datetime.datetime] = None
        self._debounce_timer = None

        # Bronnen die de forecast kunnen verversen (ook attribuutwijzigingen)
        for entity in self.solcast_entities + [self.epex_prijs_entity]:
            self.listen_state(self._bron_gewijzigd, entity, attribute="all")

        self._schedule_kwartier_timer()
        self.run_in(self._startup, 5)

    def _als_lijst(self, waarde: Any) -> List[str]:
        if waarde is None:
            return []
        if isinstance(waarde, (list, tuple)):
            return [str(w) for w in waarde if w]
        return [str(waarde)]

    def _load_configuration(self):
        for key, default in self.DEFAULT_ENTITIES.items():
            setattr(self, key, self.args.get(key, default))
        self.verbruik_plus_entities = self._als_lijst(self.args.get("verbruik_plus_entities", self.DEFAULT_PLUS_ENTITIES))
        self.verbruik_min_entities = self._als_lijst(self.args.get("verbruik_min_entities", self.DEFAULT_MIN_ENTITIES))
        solcast_extra = self._als_lijst(self.args.get("solcast_extra_entities", self.DEFAULT_SOLCAST_EXTRA_ENTITIES))
        self.solcast_entities = [self.solcast_today_entity, self.solcast_tomorrow_entity] + solcast_extra

        self.FALLBACK_EPEX_PRIJS = float(self.args.get("fallback_epex_prijs", self.FALLBACK_EPEX_PRIJS))
        self.FALLBACK_VERBRUIK_KW = float(self.args.get("fallback_verbruik_kw", self.FALLBACK_VERBRUIK_KW))
        self.MIN_VERBRUIK_KW = float(self.args.get("min_verbruik_kw", self.MIN_VERBRUIK_KW))
        self.EPEX_UNIT_MULTIPLIER = float(self.args.get("epex_unit_multiplier", self.EPEX_UNIT_MULTIPLIER))
        self.EPEX_PRIJS_IS_TOTAAL = bool(self.args.get("epex_prijs_is_totaal", self.EPEX_PRIJS_IS_TOTAAL))
        self.MAX_EPEX_FORECAST_HOURS = int(self.args.get("max_epex_forecast_hours", self.MAX_EPEX_FORECAST_HOURS))
        self.DEBUG_LOGGING = bool(self.args.get("debug_logging", self.DEBUG_LOGGING))
        self.ENERGIEBELASTING_PER_KWH = float(self.args.get("energiebelasting_per_kwh", self.ENERGIEBELASTING_PER_KWH))
        self.LEVERANCIERSKOSTEN_PER_KWH = float(self.args.get("leverancierskosten_per_kwh", self.LEVERANCIERSKOSTEN_PER_KWH))
        self.BTW_PERCENTAGE = float(self.args.get("btw_percentage", self.BTW_PERCENTAGE))
        self.SOLCAST_WAARDE_IS_KW = bool(self.args.get("solcast_waarde_is_kw", self.SOLCAST_WAARDE_IS_KW))
        self.MAX_REALISTISCH_VERBRUIK_KW = float(self.args.get("max_realistisch_verbruik_kw", self.MAX_REALISTISCH_VERBRUIK_KW))
        self.HISTORY_DAYS = int(self.args.get("history_days", self.HISTORY_DAYS))
        self.VORIG_JAAR_MARGE_DAGEN = int(self.args.get("vorig_jaar_marge_dagen", self.VORIG_JAAR_MARGE_DAGEN))
        self.VORIG_JAAR_GEWICHT = min(1.0, max(0.0, float(self.args.get("vorig_jaar_gewicht", self.VORIG_JAAR_GEWICHT))))
        self.HISTORY_REFRESH_HOURS = float(self.args.get("history_refresh_hours", self.HISTORY_REFRESH_HOURS))
        self.HISTORY_RETRY_MINUTES = float(self.args.get("history_retry_minutes", self.HISTORY_RETRY_MINUTES))
        self.HISTORY_MIN_DAGEN = int(self.args.get("history_min_dagen", self.HISTORY_MIN_DAGEN))
        self.HISTORY_START_DELAY_SECONDS = float(self.args.get("history_start_delay_seconds", self.HISTORY_START_DELAY_SECONDS))
        self.STATS_CHUNK_DAGEN_5MIN = int(self.args.get("stats_chunk_dagen_5min", self.STATS_CHUNK_DAGEN_5MIN))
        self.STATS_PAUZE_SECONDEN = float(self.args.get("stats_pauze_seconden", self.STATS_PAUZE_SECONDEN))
        self.STATS_TIMEOUT_SECONDEN = float(self.args.get("stats_timeout_seconden", self.STATS_TIMEOUT_SECONDEN))
        self.KWARTIER_DELAY_SECONDS = float(self.args.get("kwartier_delay_seconds", self.KWARTIER_DELAY_SECONDS))
        self.DEBOUNCE_SECONDS = float(self.args.get("debounce_seconds", self.DEBOUNCE_SECONDS))
        self.MIN_REBUILD_SECONDS = float(self.args.get("min_rebuild_seconds", self.MIN_REBUILD_SECONDS))
        self.LOCAL_TZ_NAME = self.args.get("local_tz", self.LOCAL_TZ_NAME)

    # ===== TIMERS EN TRIGGERS =====
    def _startup(self, kwargs):
        self._controleer_entiteiten()
        self.bouw_forecast()
        self._historie_taak_indien_nodig(self.HISTORY_START_DELAY_SECONDS)

    def _controleer_entiteiten(self):
        for entity in self.verbruik_plus_entities + self.verbruik_min_entities + self.solcast_entities + [self.epex_prijs_entity]:
            try:
                if self.get_state(entity) is None:
                    self.warning(f"Entiteit {entity} bestaat niet (controleer apps.yaml).")
            except Exception as e:
                self.warning(f"Kon entiteit {entity} niet controleren: {self._sanitize_error(e)}")

    def _schedule_kwartier_timer(self):
        nu = datetime.datetime.now(tz.UTC)
        quarter = (nu.minute // self.QUARTER_MINUTES) * self.QUARTER_MINUTES
        last_quarter = nu.replace(minute=quarter, second=0, microsecond=0)
        next_time = last_quarter + datetime.timedelta(minutes=self.QUARTER_MINUTES, seconds=self.KWARTIER_DELAY_SECONDS)
        self.run_at(self._kwartier_callback, next_time)

    def _kwartier_callback(self, kwargs):
        try:
            self._historie_taak_indien_nodig(1)
            self.bouw_forecast()
        except Exception as e:
            self.error(f"Fout in kwartier-callback: {self._sanitize_error(e)}")
        finally:
            self._schedule_kwartier_timer()

    def _bron_gewijzigd(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if self._debounce_timer is not None:
                try:
                    if self.timer_running(self._debounce_timer):
                        self.cancel_timer(self._debounce_timer)
                except Exception:
                    pass
            self._debounce_timer = self.run_in(self._bron_execute, self.DEBOUNCE_SECONDS, trigger_entity=entity)

    def _bron_execute(self, kwargs):
        trigger_entity = kwargs.get("trigger_entity", "onbekend")
        with self._lock:
            self._debounce_timer = None
            if self.last_build is not None:
                sinds = (datetime.datetime.now(tz.UTC) - self.last_build).total_seconds()
                if sinds < self.MIN_REBUILD_SECONDS:
                    if self.DEBUG_LOGGING:
                        self.log(f"[DEBUG] Bronupdate {trigger_entity} genegeerd, forecast is {sinds:.0f}s geleden gebouwd.")
                    return
            self.log(f"Bron gewijzigd ({trigger_entity}), forecast opnieuw opbouwen.")
            self.bouw_forecast()

    # ===== HULPFUNCTIES =====
    def _sanitize_error(self, error: Exception) -> str:
        error_str = str(error)
        for word in ["password", "pwd", "host", "192.168", "10.0", "172.", "secret", "token"]:
            error_str = error_str.replace(word, "[REDACTED]")
        return error_str

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

    def _floor_kwartier(self, dt: datetime.datetime) -> datetime.datetime:
        return dt.replace(minute=(dt.minute // self.QUARTER_MINUTES) * self.QUARTER_MINUTES, second=0, microsecond=0)

    def _dagtype(self, lokaal: datetime.datetime) -> str:
        return "weekend" if lokaal.weekday() >= 5 else "werkdag"

    def bereken_totaal_tarief(self, epex_prijs: float) -> float:
        totaal = epex_prijs + self.ENERGIEBELASTING_PER_KWH + self.LEVERANCIERSKOSTEN_PER_KWH
        btw_factor = 1 + (self.BTW_PERCENTAGE / 100)
        return round(totaal * btw_factor, 4)

    def _totaal_naar_epex(self, totaal: float) -> float:
        btw_factor = 1 + (self.BTW_PERCENTAGE / 100)
        return totaal / btw_factor - self.ENERGIEBELASTING_PER_KWH - self.LEVERANCIERSKOSTEN_PER_KWH

    def _zelfde_datum_vorig_jaar(self, nu_utc: datetime.datetime) -> datetime.datetime:
        lokaal = nu_utc.astimezone(self._local_tz)
        try:
            vorig = lokaal.replace(year=lokaal.year - 1)
        except ValueError:
            vorig = lokaal.replace(year=lokaal.year - 1, day=28)
        return vorig.astimezone(tz.UTC)

    # ===== STATISTICS OPHALEN (recorder.get_statistics) =====
    def _stat_tijd(self, waarde: Any) -> Optional[datetime.datetime]:
        if waarde is None:
            return None
        if isinstance(waarde, (int, float)):
            seconden = waarde / 1000.0 if waarde > 1e11 else float(waarde)
            return datetime.datetime.fromtimestamp(seconden, tz.UTC)
        return self._parse_iso_to_utc(waarde)

    def _pak_statistiek_rijen(self, res: Any, entity_id: str) -> List[Dict[str, Any]]:
        if not isinstance(res, dict):
            self._laad_problemen.add("Geen bruikbare respons van recorder.get_statistics (AppDaemon 4.5 of nieuwer nodig).")
            return []
        if res.get("success") is False or res.get("ad_status") not in (None, "OK"):
            fout = res.get("error") or res.get("ad_status")
            self._laad_problemen.add(f"recorder.get_statistics mislukt voor {entity_id}: {fout}")
            return []
        result = res.get("result")
        respons = result.get("response") if isinstance(result, dict) and "response" in result else result
        if isinstance(respons, dict) and "statistics" in respons:
            respons = respons["statistics"]
        if not isinstance(respons, dict):
            return []
        rijen = respons.get(entity_id)
        return rijen if isinstance(rijen, list) else []

    def _haal_sums(self, entity_id: str, start: datetime.datetime, einde: datetime.datetime,
                   period: str, chunk_dagen: int) -> Dict[datetime.datetime, float]:
        """Geeft per periode-einde de cumulatieve stand (sum, in kWh) terug."""
        resultaat: Dict[datetime.datetime, float] = {}
        stap = datetime.timedelta(days=max(1, chunk_dagen))
        cursor = start
        while cursor < einde:
            chunk_einde = min(cursor + stap, einde)
            res = self.call_service(
                "recorder/get_statistics",
                statistic_ids=[entity_id],
                start_time=cursor.isoformat(),
                end_time=chunk_einde.isoformat(),
                period=period,
                types=["sum"],
                units={"energy": "kWh"},
                hass_timeout=self.STATS_TIMEOUT_SECONDEN,
                timeout=self.STATS_TIMEOUT_SECONDEN + 10,
                suppress_log_messages=True,
            )
            for rij in self._pak_statistiek_rijen(res, entity_id):
                try:
                    eind = self._stat_tijd(rij.get("end"))
                    waarde = rij.get("sum")
                    if eind is None or waarde is None:
                        continue
                    resultaat[eind] = float(waarde)
                except (ValueError, TypeError, AttributeError):
                    continue
            cursor = chunk_einde
            time.sleep(self.STATS_PAUZE_SECONDEN)
        return resultaat

    def _delta_per_blok(self, sums: Dict[str, Dict[datetime.datetime, float]], blok_minuten: int) -> Dict[datetime.datetime, float]:
        """Stand_einde - Stand_begin per blok, gecombineerd: som(plus) - som(min)."""
        plus = self.verbruik_plus_entities
        minus = self.verbruik_min_entities
        alle = plus + minus
        if not alle:
            return {}

        stap = datetime.timedelta(minutes=blok_minuten)
        geldige_einden: Dict[str, Set[datetime.datetime]] = {}
        for entity in alle:
            standen = sums.get(entity) or {}
            if not standen:
                self._laad_problemen.add(f"Geen statistics voor {entity} (state_class total_increasing nodig, niet uitsluiten van recorder).")
                continue
            einden = set()
            for eind in standen:
                begin = eind - stap
                if blok_minuten == 15 and (begin.minute % 15 != 0 or eind.minute % 15 != 0):
                    continue
                if blok_minuten == 60 and (begin.minute != 0 or eind.minute != 0):
                    continue
                if begin in standen:
                    einden.add(eind)
            if not einden:
                self._laad_problemen.add(f"Geen opeenvolgende bruikbare statistics voor {entity} ({blok_minuten}-minutenblokken).")
            else:
                geldige_einden[entity] = einden

        if len(geldige_einden) != len(alle):
            return {}

        gemeenschappelijke_einden = geldige_einden[alle[0]].intersection(
            *(geldige_einden[entity] for entity in alle[1:]))
        if not gemeenschappelijke_einden:
            self._laad_problemen.add(f"Geen overlappende statistics-intervallen voor alle verbruikstellers ({blok_minuten}-minutenblokken).")
            return {}

        delta: Dict[datetime.datetime, float] = {}
        for eind in sorted(gemeenschappelijke_einden):
            begin = eind - stap
            totaal = 0.0
            for entity in plus:
                s = sums[entity]
                totaal += s[eind] - s[begin]
            for entity in minus:
                s = sums[entity]
                totaal -= s[eind] - s[begin]
            delta[begin] = totaal
        return delta

    def _bouw_kwartier_kwh(self, start: datetime.datetime, einde: datetime.datetime,
                           probe_5min: bool) -> Tuple[Dict[datetime.datetime, float], str]:
        """kWh per kwartier uit tellerstanden: 5-minuutstatistics waar beschikbaar, anders uurwaarden / 4."""
        entities = self.verbruik_plus_entities + self.verbruik_min_entities
        uur_sums = {e: self._haal_sums(e, start, einde, "hour", self.STATS_CHUNK_DAGEN_UUR) for e in entities}

        gebruik_5min = True
        if probe_5min and entities:
            midden = start + (einde - start) / 2
            probe = self._haal_sums(entities[0], midden, midden + datetime.timedelta(days=1), "5minute", 1)
            gebruik_5min = len(probe) >= self.STATS_PROBE_MIN_RIJEN

        uur_kwartieren: Dict[datetime.datetime, float] = {}
        for uurstart, kwh in self._delta_per_blok(uur_sums, 60).items():
            for i in range(4):
                uur_kwartieren[uurstart + datetime.timedelta(minutes=i * self.QUARTER_MINUTES)] = kwh / 4.0

        vijf_kwartieren: Dict[datetime.datetime, float] = {}
        if gebruik_5min:
            vijf_sums = {e: self._haal_sums(e, start, einde, "5minute", self.STATS_CHUNK_DAGEN_5MIN) for e in entities}
            vijf_kwartieren = self._delta_per_blok(vijf_sums, 15)

        kwartieren = dict(uur_kwartieren)
        kwartieren.update(vijf_kwartieren)
        n_uur = sum(1 for q in uur_kwartieren if q not in vijf_kwartieren)
        return kwartieren, f"5min={len(vijf_kwartieren)}, uur={n_uur}"

    # ===== PROFIELEN =====
    def _bouw_profiel(self, kwartier_kwh: Dict[datetime.datetime, float]) -> Tuple[Dict[Tuple, float], int, int]:
        max_kwh = self.MAX_REALISTISCH_VERBRUIK_KW * self.QUARTER_HOURS
        min_kwh = self.MIN_VERBRUIK_KW * self.QUARTER_HOURS
        waarden_alle: Dict[Tuple, List[float]] = {}
        waarden_type: Dict[Tuple, List[float]] = {}
        dagen_type: Dict[Tuple, Set[datetime.date]] = {}
        totaal = 0
        uitgesloten = 0
        for q_start, kwh in kwartier_kwh.items():
            totaal += 1
            kwh = max(0.0, kwh)
            if kwh > max_kwh:
                uitgesloten += 1
                continue
            lokaal = q_start.astimezone(self._local_tz)
            blok = (lokaal.hour, lokaal.minute // self.QUARTER_MINUTES)
            type_key = (self._dagtype(lokaal),) + blok
            waarden_alle.setdefault(blok, []).append(kwh)
            waarden_type.setdefault(type_key, []).append(kwh)
            dagen_type.setdefault(type_key, set()).add(lokaal.date())
        profiel: Dict[Tuple, float] = {}
        for blok, vals in waarden_alle.items():
            profiel[("alle",) + blok] = max(min_kwh, sum(vals) / len(vals))
        for type_key, vals in waarden_type.items():
            if len(dagen_type.get(type_key, set())) >= self.HISTORY_MIN_DAGEN:
                profiel[type_key] = max(min_kwh, sum(vals) / len(vals))
        return profiel, totaal, uitgesloten

    @staticmethod
    def _zoek_profiel(profiel: Dict[Tuple, float], dagtype: str, blok: Tuple[int, int]) -> Optional[float]:
        for key in ((dagtype,) + blok, ("alle",) + blok):
            if key in profiel:
                return profiel[key]
        return None

    def get_db_historie(self, slot_utc: datetime.datetime) -> float:
        """Verwacht huisverbruik in kWh voor dit kwartier (recent profiel gemengd met vorig jaar)."""
        lokaal = slot_utc.astimezone(self._local_tz)
        blok = (lokaal.hour, lokaal.minute // self.QUARTER_MINUTES)
        dagtype = self._dagtype(lokaal)
        recent = self._zoek_profiel(self.profiel_recent, dagtype, blok)
        vorig = self._zoek_profiel(self.profiel_vorig_jaar, dagtype, blok)
        if recent is not None and vorig is not None:
            return (1.0 - self.VORIG_JAAR_GEWICHT) * recent + self.VORIG_JAAR_GEWICHT * vorig
        if recent is not None:
            return recent
        if vorig is not None:
            return vorig
        return self.FALLBACK_VERBRUIK_KW * self.QUARTER_HOURS

    # ===== HISTORIE LADEN (buiten de hoofdlock, zodat de forecast niet stokt) =====
    def _historie_taak_indien_nodig(self, vertraging: float):
        nu = datetime.datetime.now(tz.UTC)
        with self._lock:
            if self._historie_bezig:
                return
            if self._history_next_try is not None and nu < self._history_next_try:
                return
            if self.historie_geladen_op is not None:
                leeftijd_uren = (nu - self.historie_geladen_op).total_seconds() / 3600.0
                if leeftijd_uren < self.HISTORY_REFRESH_HOURS:
                    return
            self._historie_bezig = True
        self.run_in(self._historie_callback, vertraging)

    def _historie_callback(self, kwargs):
        try:
            self._laad_verbruikshistorie()
        except Exception as e:
            fout = self._sanitize_error(e)
            self.error(f"Fout bij laden verbruikshistorie: {fout}")
            with self._lock:
                self._history_next_try = datetime.datetime.now(tz.UTC) + datetime.timedelta(minutes=self.HISTORY_RETRY_MINUTES)
                self._historie_waarschuwingen = [
                    f"Historie vernieuwen mislukt ({fout}); laatst bekende profiel blijft actief waar beschikbaar."
                ]
        finally:
            with self._lock:
                self._historie_bezig = False
            self.bouw_forecast()

    def _laad_verbruikshistorie(self):
        nu = datetime.datetime.now(tz.UTC)
        retry = nu + datetime.timedelta(minutes=self.HISTORY_RETRY_MINUTES)
        self._laad_problemen = set()
        if not self.verbruik_plus_entities:
            self.error("Geen verbruik_plus_entities geconfigureerd, gebruik fallbackverbruik.")
            with self._lock:
                self._history_next_try = retry
            return

        recent_q, recent_bron = self._bouw_kwartier_kwh(nu - datetime.timedelta(days=self.HISTORY_DAYS), nu, probe_5min=False)
        centrum = self._zelfde_datum_vorig_jaar(nu)
        marge = datetime.timedelta(days=self.VORIG_JAAR_MARGE_DAGEN)
        vorig_q, vorig_bron = self._bouw_kwartier_kwh(centrum - marge, centrum + marge, probe_5min=True)

        profiel_recent, r_totaal, r_uit = self._bouw_profiel(recent_q)
        profiel_vorig, v_totaal, v_uit = self._bouw_profiel(vorig_q)

        waarschuwingen = sorted(self._laad_problemen)
        totaal = r_totaal + v_totaal
        uitgesloten = r_uit + v_uit
        if totaal > 0 and uitgesloten / totaal > 0.05:
            waarschuwingen.append(
                f"{uitgesloten} van {totaal} verbruikskwartieren ({uitgesloten / totaal * 100:.0f}%) boven "
                f"{self.MAX_REALISTISCH_VERBRUIK_KW:.0f} kW gemiddeld uitgesloten. Controleer de verbruik-entiteiten (eenheid kWh).")
        elif uitgesloten > 0:
            self.log(f"{uitgesloten} onrealistische verbruikskwartieren uitgesloten van de historie.")

        if not profiel_recent and not profiel_vorig:
            self.warning("Verbruikshistorie uit statistics bevat geen bruikbare kwartieren, gebruik fallback.")
            with self._lock:
                self._history_next_try = retry
                waarschuwingen.append("Geen nieuwe bruikbare historie geladen; laatst bekende profielen blijven actief waar beschikbaar.")
                self._historie_waarschuwingen = waarschuwingen
            return

        with self._lock:
            if profiel_recent:
                self.profiel_recent = profiel_recent
            else:
                waarschuwingen.append("Geen nieuw recent profiel geladen; laatst bekende profiel blijft actief waar beschikbaar.")
            if profiel_vorig:
                self.profiel_vorig_jaar = profiel_vorig
            else:
                waarschuwingen.append("Geen nieuw profiel van vorig jaar geladen; laatst bekende profiel blijft actief waar beschikbaar.")
            if profiel_recent and profiel_vorig:
                self.historie_geladen_op = nu
                self._history_next_try = None
            else:
                self._history_next_try = retry
            self._historie_waarschuwingen = waarschuwingen
            if profiel_recent:
                self._historie_bronnen["recent"] = recent_bron
            if profiel_vorig:
                self._historie_bronnen["vorig_jaar"] = vorig_bron
        n_recent, n_vorig = self._tel_historie_blokken()
        self.log(f"Verbruikshistorie geladen uit statistics: recent {n_recent} profielblokken ({recent_bron}), "
                 f"vorig jaar {n_vorig} profielblokken ({vorig_bron}).")

    def _tel_historie_blokken(self) -> Tuple[int, int]:
        n_recent = sum(1 for k in self.profiel_recent if k[0] == "alle")
        n_vorig = sum(1 for k in self.profiel_vorig_jaar if k[0] == "alle")
        return n_recent, n_vorig

    # ===== SOLCAST =====
    def _build_forecast_dict(self, waarschuwingen: List[str]):
        """Vult forecast_dict met PV-opbrengst in kWh per kwartier."""
        self.forecast_dict = {}
        fc: List[Dict[str, Any]] = []
        gevonden = False
        for entity in self.solcast_entities:
            toestand = self.get_state(entity, attribute="all")
            if not toestand:
                continue
            gevonden = True
            fc.extend((toestand.get("attributes", {}) or {}).get("forecasts", []) or [])
        if not gevonden:
            waarschuwingen.append("Geen Solcast forecasts beschikbaar, PV = 0.")
            return
        # Solcast levert per 30 minuten: kWh per half uur / 2, of kW (gemiddeld vermogen) / 4 = kWh per kwartier
        deler = 4.0 if self.SOLCAST_WAARDE_IS_KW else 2.0
        ongeldig = 0
        for f in fc:
            try:
                period_end = self._parse_iso_to_utc(f["period_end"])
                period_start = period_end - datetime.timedelta(minutes=30)
                pv_kwh = max(0.0, float(f.get("pv_estimate", 0.0) or 0.0)) / deler
                for i in range(2):
                    kwartier_start = period_start + datetime.timedelta(minutes=i * self.QUARTER_MINUTES)
                    self.forecast_dict[kwartier_start] = pv_kwh
            except Exception as e:
                ongeldig += 1
                if self.DEBUG_LOGGING:
                    self.log(f"[DEBUG] Ongeldige Solcast-periode: {self._sanitize_error(e)}")
        if ongeldig:
            waarschuwingen.append("Solcast-perioden met ongeldige data overgeslagen.")
        if not self.forecast_dict:
            waarschuwingen.append("Solcast bevat geen bruikbare periodes, PV = 0.")

    # ===== EPEX (EpexPredictor REST-sensor: attributen s = unix-tijden, t = prijzen) =====
    def _build_prijs_dict(self, waarschuwingen: List[str]):
        self.prijs_dict = {}
        p = self.get_state(self.epex_prijs_entity, attribute="all")
        if not p:
            waarschuwingen.append("Geen EPEX prijzen beschikbaar, fallbackprijs gebruikt.")
            return
        attrs = p.get("attributes", {}) or {}
        s_lijst = attrs.get("s")
        t_lijst = attrs.get("t")
        if not isinstance(s_lijst, list) or not isinstance(t_lijst, list) or not s_lijst or len(s_lijst) != len(t_lijst):
            waarschuwingen.append("EPEX-sensor mist geldige attributen 's' en 't', fallbackprijs gebruikt.")
            return
        ongeldig = 0
        vorige_duur = 900.0
        aantal = len(s_lijst)
        for i in range(aantal):
            try:
                start_ts = float(s_lijst[i])
                if i + 1 < aantal:
                    duur = float(s_lijst[i + 1]) - start_ts
                else:
                    duur = vorige_duur
                if duur <= 0:
                    duur = 900.0
                vorige_duur = duur
                aantal_kwartieren = max(1, min(4, int(round(duur / 900.0))))
                prijs = float(t_lijst[i]) * self.EPEX_UNIT_MULTIPLIER
                if self.EPEX_PRIJS_IS_TOTAAL:
                    prijs = self._totaal_naar_epex(prijs)
                start = self._floor_kwartier(datetime.datetime.fromtimestamp(start_ts, tz.UTC))
                for k in range(aantal_kwartieren):
                    self.prijs_dict[start + datetime.timedelta(minutes=k * self.QUARTER_MINUTES)] = prijs
            except (ValueError, TypeError, OverflowError, OSError) as e:
                ongeldig += 1
                if self.DEBUG_LOGGING:
                    self.log(f"[DEBUG] Ongeldige EPEX prijs: {self._sanitize_error(e)}")
        if ongeldig:
            waarschuwingen.append("EPEX-prijzen met ongeldige data overgeslagen.")
        if not self.prijs_dict:
            waarschuwingen.append("EPEX bevat geen bruikbare prijzen, fallbackprijs gebruikt.")

    # ===== WAARSCHUWINGEN (ALLEEN LOGGEN BIJ VERANDERING) =====
    def _log_waarschuwingen_bij_verandering(self, waarschuwingen: List[str]):
        huidig = set(waarschuwingen)
        for w in sorted(huidig - self._gelogde_waarschuwingen):
            self.warning(f"Forecast waarschuwing: {w}")
        for w in sorted(self._gelogde_waarschuwingen - huidig):
            self.log(f"Forecast waarschuwing opgelost: {w}")
        self._gelogde_waarschuwingen = huidig

    # ===== MATRIX OPBOUWEN EN PUBLICEREN =====
    def bouw_forecast(self):
        with self._lock:
            try:
                nu = datetime.datetime.now(tz.UTC)
                waarschuwingen: List[str] = []
                self._build_forecast_dict(waarschuwingen)
                self._build_prijs_dict(waarschuwingen)
                if not self.profiel_recent and not self.profiel_vorig_jaar:
                    waarschuwingen.append("Geen verbruikshistorie uit statistics, fallbackverbruik gebruikt.")
                waarschuwingen.extend(self._historie_waarschuwingen)

                eerste_slot = self._floor_kwartier(nu)
                aantal_slots = max(1, int(self.MAX_EPEX_FORECAST_HOURS * 60 / self.QUARTER_MINUTES))
                matrix: List[Dict[str, Any]] = []
                fallback_slots = 0
                pv_ontbrekende_slots = 0
                laatste_prijs_slot: Optional[datetime.datetime] = None
                laatste_pv_slot: Optional[datetime.datetime] = None

                for i in range(aantal_slots):
                    slot = eerste_slot + datetime.timedelta(minutes=i * self.QUARTER_MINUTES)
                    pv_kwh = self.forecast_dict.get(slot)
                    if pv_kwh is not None:
                        laatste_pv_slot = slot
                    else:
                        pv_kwh = 0.0
                        pv_ontbrekende_slots += 1
                    verbruik_kwh = self.get_db_historie(slot)
                    epex_prijs = self.prijs_dict.get(slot)
                    if epex_prijs is None:
                        epex_prijs = self.FALLBACK_EPEX_PRIJS
                        prijs_bron = "fallback"
                        fallback_slots += 1
                    else:
                        prijs_bron = "epex"
                        laatste_prijs_slot = slot
                    matrix.append({
                        "tijd": slot.isoformat(),
                        "pv_kwh": round(pv_kwh, 4),
                        "verbruik_kwh": round(verbruik_kwh, 4),
                        "pv_kw": round(pv_kwh / self.QUARTER_HOURS, 3),
                        "verbruik_kw": round(verbruik_kwh / self.QUARTER_HOURS, 3),
                        "epex_prijs": round(epex_prijs, 4),
                        "net_prijs": self.bereken_totaal_tarief(epex_prijs),
                        "prijs_bron": prijs_bron,
                    })

                self.matrix_kwartieren = matrix
                self.last_build = nu

                huidig_kwartier_fallback = bool(matrix) and matrix[0]["prijs_bron"] == "fallback"
                if not self.prijs_dict or not self.forecast_dict or huidig_kwartier_fallback:
                    status = "degraded"
                elif (fallback_slots > 0 or pv_ontbrekende_slots > 0 or self._historie_waarschuwingen
                      or (not self.profiel_recent and not self.profiel_vorig_jaar)):
                    status = "partial"
                else:
                    status = "ok"

                if pv_ontbrekende_slots and self.forecast_dict:
                    waarschuwingen.append(
                        f"PV-data ontbreekt voor {pv_ontbrekende_slots} van {aantal_slots} forecastkwartieren; ontbrekende waarden zijn 0 kWh."
                    )

                n_recent, n_vorig = self._tel_historie_blokken()
                einde_slot = eerste_slot + datetime.timedelta(minutes=aantal_slots * self.QUARTER_MINUTES)
                kwartier = datetime.timedelta(minutes=self.QUARTER_MINUTES)
                attributes = {
                    "friendly_name": "Energie Core Forecast",
                    "icon": "mdi:chart-timeline-variant",
                    "device_class": "timestamp",
                    "schema_versie": self.SCHEMA_VERSIE,
                    "matrix_eenheid": "kwh_per_kwartier",
                    "status": status,
                    "waarschuwingen": waarschuwingen,
                    "generated_at": nu.isoformat(),
                    "horizon_uren": self.MAX_EPEX_FORECAST_HOURS,
                    "horizon_start": eerste_slot.isoformat(),
                    "horizon_einde": einde_slot.isoformat(),
                    "kwartier_minuten": self.QUARTER_MINUTES,
                    "slot_count": len(matrix),
                    "fallback_prijs_slots": fallback_slots,
                    "pv_ontbrekende_slots": pv_ontbrekende_slots,
                    "prijs_dekking_tot": (laatste_prijs_slot + kwartier).isoformat() if laatste_prijs_slot else None,
                    "pv_dekking_tot": (laatste_pv_slot + kwartier).isoformat() if laatste_pv_slot else None,
                    "verbruik_plus_entities": self.verbruik_plus_entities,
                    "verbruik_min_entities": self.verbruik_min_entities,
                    "historie_geladen_op": self.historie_geladen_op.isoformat() if self.historie_geladen_op else None,
                    "historie_blokken": n_recent,
                    "historie_vorig_jaar_blokken": n_vorig,
                    "historie_bronnen": self._historie_bronnen,
                    "vorig_jaar_gewicht": self.VORIG_JAAR_GEWICHT,
                    "tarief": {
                        "energiebelasting": self.ENERGIEBELASTING_PER_KWH,
                        "leverancierskosten": self.LEVERANCIERSKOSTEN_PER_KWH,
                        "btw": self.BTW_PERCENTAGE,
                    },
                    "forecast": matrix,
                }
                self.set_state(self.forecast_entity, state=nu.isoformat(), attributes=attributes)
                self.log(f"Forecast gepubliceerd: {len(matrix)} kwartieren, status={status}, "
                         f"fallbackprijzen={fallback_slots}.")
                self._log_waarschuwingen_bij_verandering(waarschuwingen)
            except Exception as e:
                self.error(f"Fout in bouw_forecast: {self._sanitize_error(e)}")