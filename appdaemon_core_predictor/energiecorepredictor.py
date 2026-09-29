import hassapi as hass
import datetime
import threading
from typing import Dict, List, Optional, Any, Tuple, Set
from dateutil import tz
from dateutil.parser import parse


class EnergieCorePredictor(hass.Hass):
    """
    Zelfstandige voorspeller voor huisverbruik, Solcast-opbrengst en EPEX-prijzen.
    Bouwt elk kwartier de 'matrix_kwartieren' en schrijft die als lijst naar het
    attribuut 'forecast' van sensor.energie_core_forecast.
    """

    # ===== CONFIGURATIE =====
    SCHEMA_VERSIE = 1
    QUARTER_MINUTES = 15
    FALLBACK_EPEX_PRIJS = 0.20
    FALLBACK_VERBRUIK_KW = 0.11
    MIN_VERBRUIK_KW = 0.04
    EPEX_UNIT_MULTIPLIER = 1.0
    MAX_EPEX_FORECAST_HOURS = 48
    DEBUG_LOGGING = False

    # Belastingen (2026)
    ENERGIEBELASTING_PER_KWH = 0.15
    LEVERANCIERSKOSTEN_PER_KWH = 0.03
    BTW_PERCENTAGE = 21.0

    # Solcast: True = pv_estimate is gemiddeld vermogen in kW (standaard Solcast-integratie).
    # False = pv_estimate is kWh per 30 minuten (oud gedrag van emslaadpaalv10).
    SOLCAST_WAARDE_IS_KW = False

    # Verbruikssensor: "auto" (leest unit_of_measurement), "W" of "kW".
    # Kan de eenheid niet bepaald worden, dan wordt W aangenomen.
    VERBRUIK_EENHEID = "auto"
    MAX_REALISTISCH_VERBRUIK_KW = 30.0

    # Historie
    HISTORY_DAYS = 30
    HISTORY_REFRESH_HOURS = 6
    HISTORY_RETRY_MINUTES = 30
    HISTORY_MIN_DAGEN = 2

    # Timers
    KWARTIER_DELAY_SECONDS = 0
    DEBOUNCE_SECONDS = 10
    MIN_REBUILD_SECONDS = 30

    LOCAL_TZ_NAME = "Europe/Amsterdam"

    # ===== ENTITY CONFIGURATIE =====
    DEFAULT_ENTITIES = {
        "verbruik_huis_entity": "sensor.vermogen_huis_excl_stuurbaar",
        "solcast_today_entity": "sensor.solcast_pv_forecast_forecast_today",
        "solcast_tomorrow_entity": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "epex_prijs_entity": "sensor.nordpool_huidige_kwartierprijs_nl",
        "forecast_entity": "sensor.energie_core_forecast",
    }

    def initialize(self):
        self.log("Energie Core Predictor v2 opgestart.")
        self._lock = threading.RLock()
        self._load_configuration()
        self._local_tz = tz.gettz(self.LOCAL_TZ_NAME)

        self.db_cache: Dict[Tuple, float] = {}
        self.db_cache_loaded_at: Optional[datetime.datetime] = None
        self._history_next_try: Optional[datetime.datetime] = None
        self._historie_waarschuwing: Optional[str] = None
        self._verbruik_eenheid_gebruikt: Optional[str] = None
        self._gelogde_waarschuwingen: Set[str] = set()
        self.prijs_dict: Dict[datetime.datetime, float] = {}
        self.forecast_dict: Dict[datetime.datetime, float] = {}
        self.matrix_kwartieren: List[Dict[str, Any]] = []
        self.last_build: Optional[datetime.datetime] = None
        self._debounce_timer = None

        # Bronnen die de forecast kunnen verversen (ook attribuutwijzigingen)
        self.listen_state(self._bron_gewijzigd, self.solcast_today_entity, attribute="all")
        self.listen_state(self._bron_gewijzigd, self.solcast_tomorrow_entity, attribute="all")
        self.listen_state(self._bron_gewijzigd, self.epex_prijs_entity, attribute="all")

        self._schedule_kwartier_timer()
        self.run_in(self._startup, 5)

    def _load_configuration(self):
        for key, default in self.DEFAULT_ENTITIES.items():
            setattr(self, key, self.args.get(key, default))
        self.FALLBACK_EPEX_PRIJS = float(self.args.get("fallback_epex_prijs", self.FALLBACK_EPEX_PRIJS))
        self.FALLBACK_VERBRUIK_KW = float(self.args.get("fallback_verbruik_kw", self.FALLBACK_VERBRUIK_KW))
        self.MIN_VERBRUIK_KW = float(self.args.get("min_verbruik_kw", self.MIN_VERBRUIK_KW))
        self.EPEX_UNIT_MULTIPLIER = float(self.args.get("epex_unit_multiplier", self.EPEX_UNIT_MULTIPLIER))
        self.MAX_EPEX_FORECAST_HOURS = int(self.args.get("max_epex_forecast_hours", self.MAX_EPEX_FORECAST_HOURS))
        self.DEBUG_LOGGING = bool(self.args.get("debug_logging", self.DEBUG_LOGGING))
        self.ENERGIEBELASTING_PER_KWH = float(self.args.get("energiebelasting_per_kwh", self.ENERGIEBELASTING_PER_KWH))
        self.LEVERANCIERSKOSTEN_PER_KWH = float(self.args.get("leverancierskosten_per_kwh", self.LEVERANCIERSKOSTEN_PER_KWH))
        self.BTW_PERCENTAGE = float(self.args.get("btw_percentage", self.BTW_PERCENTAGE))
        self.SOLCAST_WAARDE_IS_KW = bool(self.args.get("solcast_waarde_is_kw", self.SOLCAST_WAARDE_IS_KW))
        self.VERBRUIK_EENHEID = str(self.args.get("verbruik_eenheid", self.VERBRUIK_EENHEID))
        self.MAX_REALISTISCH_VERBRUIK_KW = float(self.args.get("max_realistisch_verbruik_kw", self.MAX_REALISTISCH_VERBRUIK_KW))
        self.HISTORY_DAYS = int(self.args.get("history_days", self.HISTORY_DAYS))
        self.HISTORY_REFRESH_HOURS = float(self.args.get("history_refresh_hours", self.HISTORY_REFRESH_HOURS))
        self.HISTORY_RETRY_MINUTES = float(self.args.get("history_retry_minutes", self.HISTORY_RETRY_MINUTES))
        self.HISTORY_MIN_DAGEN = int(self.args.get("history_min_dagen", self.HISTORY_MIN_DAGEN))
        self.KWARTIER_DELAY_SECONDS = float(self.args.get("kwartier_delay_seconds", self.KWARTIER_DELAY_SECONDS))
        self.DEBOUNCE_SECONDS = float(self.args.get("debounce_seconds", self.DEBOUNCE_SECONDS))
        self.MIN_REBUILD_SECONDS = float(self.args.get("min_rebuild_seconds", self.MIN_REBUILD_SECONDS))
        self.LOCAL_TZ_NAME = self.args.get("local_tz", self.LOCAL_TZ_NAME)

    # ===== TIMERS EN TRIGGERS =====
    def _startup(self, kwargs):
        with self._lock:
            self._ververs_historie_indien_nodig()
            self.bouw_forecast()

    def _schedule_kwartier_timer(self):
        nu = datetime.datetime.now(tz.UTC)
        quarter = (nu.minute // self.QUARTER_MINUTES) * self.QUARTER_MINUTES
        last_quarter = nu.replace(minute=quarter, second=0, microsecond=0)
        next_time = last_quarter + datetime.timedelta(minutes=self.QUARTER_MINUTES, seconds=self.KWARTIER_DELAY_SECONDS)
        self.run_at(self._kwartier_callback, next_time)

    def _kwartier_callback(self, kwargs):
        try:
            with self._lock:
                self._ververs_historie_indien_nodig()
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

    def _extract_timestamp(self, state_dict: Any) -> Optional[datetime.datetime]:
        if not isinstance(state_dict, dict):
            try:
                state_dict = dict(state_dict)
            except (TypeError, ValueError):
                return None
        for key in ["last_changed", "last_reported", "last_updated", "last_seen"]:
            if key in state_dict:
                try:
                    dt = parse(str(state_dict[key]))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz.UTC)
                    else:
                        dt = dt.astimezone(tz.UTC)
                    return dt
                except (ValueError, TypeError, OverflowError):
                    continue
        return None

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

    # ===== VERBRUIKSHISTORIE =====
    def _bepaal_verbruik_factor(self) -> float:
        """Geeft de factor waarmee sensorwaarden naar kW omgerekend worden."""
        eenheid = str(self.VERBRUIK_EENHEID).strip().lower()
        if eenheid == "auto":
            attr = self.get_state(self.verbruik_huis_entity, attribute="unit_of_measurement")
            eenheid = str(attr).strip().lower() if attr else ""
            if eenheid not in ("w", "kw"):
                self.warning(f"Eenheid van {self.verbruik_huis_entity} niet te bepalen ('{attr}'), aanname: W.")
                eenheid = "w"
        elif eenheid not in ("w", "kw"):
            self.error(f"Ongeldige verbruik_eenheid '{self.VERBRUIK_EENHEID}', aanname: W.")
            eenheid = "w"
        if eenheid == "kw":
            self._verbruik_eenheid_gebruikt = "kW"
            return 1.0
        self._verbruik_eenheid_gebruikt = "W"
        return 0.001

    def _ververs_historie_indien_nodig(self):
        nu = datetime.datetime.now(tz.UTC)
        if self._history_next_try is not None and nu < self._history_next_try:
            return
        if self.db_cache_loaded_at is not None:
            leeftijd_uren = (nu - self.db_cache_loaded_at).total_seconds() / 3600.0
            if leeftijd_uren < self.HISTORY_REFRESH_HOURS:
                return
        self._laad_verbruikshistorie()

    def _laad_verbruikshistorie(self):
        nu = datetime.datetime.now(tz.UTC)
        start = nu - datetime.timedelta(days=self.HISTORY_DAYS)
        retry = nu + datetime.timedelta(minutes=self.HISTORY_RETRY_MINUTES)
        try:
            factor = self._bepaal_verbruik_factor()
            data = self.get_history(entity_id=self.verbruik_huis_entity, start_time=start, end_time=nu)
            if not data:
                self.warning("Geen verbruikshistorie beschikbaar, gebruik fallback.")
                self._history_next_try = retry
                return
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                records = data[0]
            elif isinstance(data, list):
                records = data
            else:
                records = [data]

            waarden_alle: Dict[Tuple, List[float]] = {}
            waarden_type: Dict[Tuple, List[float]] = {}
            dagen_type: Dict[Tuple, Set[datetime.date]] = {}
            totaal_metingen = 0
            uitgesloten = 0
            for s in records:
                try:
                    ts = self._extract_timestamp(s)
                    if ts is None:
                        continue
                    val = float(s["state"]) * factor
                    if val != val:
                        continue
                    val = max(0.0, val)
                    totaal_metingen += 1
                    if val > self.MAX_REALISTISCH_VERBRUIK_KW:
                        uitgesloten += 1
                        continue
                    lokaal = ts.astimezone(self._local_tz)
                    blok = (lokaal.hour, lokaal.minute // self.QUARTER_MINUTES)
                    type_key = (self._dagtype(lokaal),) + blok
                    waarden_alle.setdefault(blok, []).append(val)
                    waarden_type.setdefault(type_key, []).append(val)
                    dagen_type.setdefault(type_key, set()).add(lokaal.date())
                except (ValueError, KeyError, TypeError):
                    continue

            # Onrealistische metingen zijn uitgesloten; kwartieren zonder geldige metingen vallen terug op fallback
            self._historie_waarschuwing = None
            if uitgesloten > 0:
                aandeel = uitgesloten / totaal_metingen
                if aandeel > 0.05 or not waarden_alle:
                    self._historie_waarschuwing = (
                        f"{uitgesloten} van {totaal_metingen} verbruiksmetingen ({aandeel * 100:.0f}%) boven "
                        f"{self.MAX_REALISTISCH_VERBRUIK_KW:.0f} kW uitgesloten. "
                        f"Controleer verbruik_eenheid (nu: {self._verbruik_eenheid_gebruikt}).")
                else:
                    self.log(f"{uitgesloten} onrealistische verbruiksmetingen (> {self.MAX_REALISTISCH_VERBRUIK_KW:.0f} kW) "
                             f"uitgesloten van de historie.")

            if not waarden_alle:
                self.warning("Verbruikshistorie bevat geen bruikbare waarden, gebruik fallback.")
                self._history_next_try = retry
                return

            nieuwe_cache: Dict[Tuple, float] = {}
            for blok, vals in waarden_alle.items():
                nieuwe_cache[("alle",) + blok] = max(self.MIN_VERBRUIK_KW, sum(vals) / len(vals))
            for type_key, vals in waarden_type.items():
                if len(dagen_type.get(type_key, set())) >= self.HISTORY_MIN_DAGEN:
                    nieuwe_cache[type_key] = max(self.MIN_VERBRUIK_KW, sum(vals) / len(vals))

            self.db_cache = nieuwe_cache
            self.db_cache_loaded_at = nu
            self._history_next_try = None
            n_alle, n_werkdag, n_weekend = self._tel_historie_blokken()
            self.log(f"Verbruikshistorie geladen van {self.verbruik_huis_entity} (eenheid {self._verbruik_eenheid_gebruikt}): "
                     f"{n_alle} kwartierblokken, profielen werkdag={n_werkdag}, weekend={n_weekend}.")
        except Exception as e:
            self.error(f"Fout bij laden verbruikshistorie: {self._sanitize_error(e)}")
            self._history_next_try = retry

    def _tel_historie_blokken(self) -> Tuple[int, int, int]:
        n_alle = sum(1 for k in self.db_cache if k[0] == "alle")
        n_werkdag = sum(1 for k in self.db_cache if k[0] == "werkdag")
        n_weekend = sum(1 for k in self.db_cache if k[0] == "weekend")
        return n_alle, n_werkdag, n_weekend

    def get_db_historie(self, slot_utc: datetime.datetime) -> float:
        lokaal = slot_utc.astimezone(self._local_tz)
        blok = (lokaal.hour, lokaal.minute // self.QUARTER_MINUTES)
        for key in ((self._dagtype(lokaal),) + blok, ("alle",) + blok):
            if key in self.db_cache:
                return self.db_cache[key]
        return self.FALLBACK_VERBRUIK_KW

    # ===== SOLCAST =====
    def _build_forecast_dict(self, waarschuwingen: List[str]):
        self.forecast_dict = {}
        solcast_today = self.get_state(self.solcast_today_entity, attribute="all")
        solcast_tomorrow = self.get_state(self.solcast_tomorrow_entity, attribute="all")
        if not solcast_today and not solcast_tomorrow:
            waarschuwingen.append("Geen Solcast forecasts beschikbaar, PV = 0.")
            return
        fc = []
        if solcast_today:
            fc.extend((solcast_today.get("attributes", {}) or {}).get("forecasts", []) or [])
        if solcast_tomorrow:
            fc.extend((solcast_tomorrow.get("attributes", {}) or {}).get("forecasts", []) or [])
        factor = 1.0 if self.SOLCAST_WAARDE_IS_KW else 2.0
        ongeldig = 0
        for f in fc:
            try:
                period_end = self._parse_iso_to_utc(f["period_end"])
                period_start = period_end - datetime.timedelta(minutes=30)
                pv_kw = max(0.0, float(f.get("pv_estimate", 0.0) or 0.0)) * factor
                for i in range(2):
                    kwartier_start = period_start + datetime.timedelta(minutes=i * self.QUARTER_MINUTES)
                    self.forecast_dict[kwartier_start] = pv_kw
            except Exception as e:
                ongeldig += 1
                if self.DEBUG_LOGGING:
                    self.log(f"[DEBUG] Ongeldige Solcast-periode: {self._sanitize_error(e)}")
        if ongeldig:
            waarschuwingen.append("Solcast-perioden met ongeldige data overgeslagen.")
        if not self.forecast_dict:
            waarschuwingen.append("Solcast bevat geen bruikbare periodes, PV = 0.")

    # ===== EPEX =====
    def _build_prijs_dict(self, waarschuwingen: List[str]):
        self.prijs_dict = {}
        p = self.get_state(self.epex_prijs_entity, attribute="all")
        if not p:
            waarschuwingen.append("Geen EPEX prijzen beschikbaar, fallbackprijs gebruikt.")
            return
        attrs = p.get("attributes", {}) or {}
        raw = list(attrs.get("raw_today") or []) + list(attrs.get("raw_tomorrow") or [])
        ongeldig = 0
        for r in raw:
            try:
                start = self._parse_iso_to_utc(r["start"])
                if r.get("end"):
                    einde = self._parse_iso_to_utc(r["end"])
                else:
                    einde = start + datetime.timedelta(minutes=self.QUARTER_MINUTES)
                if einde <= start:
                    einde = start + datetime.timedelta(minutes=self.QUARTER_MINUTES)
                prijs = float(r["value"]) * self.EPEX_UNIT_MULTIPLIER
                slot = self._floor_kwartier(start)
                while slot < einde:
                    self.prijs_dict[slot] = prijs
                    slot += datetime.timedelta(minutes=self.QUARTER_MINUTES)
            except Exception as e:
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
                if not self.db_cache:
                    waarschuwingen.append("Geen verbruikshistorie, fallbackverbruik gebruikt.")
                if self._historie_waarschuwing:
                    waarschuwingen.append(self._historie_waarschuwing)

                eerste_slot = self._floor_kwartier(nu)
                aantal_slots = max(1, int(self.MAX_EPEX_FORECAST_HOURS * 60 / self.QUARTER_MINUTES))
                matrix: List[Dict[str, Any]] = []
                fallback_slots = 0
                laatste_prijs_slot: Optional[datetime.datetime] = None
                laatste_pv_slot: Optional[datetime.datetime] = None

                for i in range(aantal_slots):
                    slot = eerste_slot + datetime.timedelta(minutes=i * self.QUARTER_MINUTES)
                    pv_kw = self.forecast_dict.get(slot)
                    if pv_kw is not None:
                        laatste_pv_slot = slot
                    else:
                        pv_kw = 0.0
                    verbruik_kw = self.get_db_historie(slot)
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
                        "pv_kw": round(pv_kw, 3),
                        "verbruik_kw": round(verbruik_kw, 3),
                        "epex_prijs": round(epex_prijs, 4),
                        "net_prijs": self.bereken_totaal_tarief(epex_prijs),
                        "prijs_bron": prijs_bron,
                    })

                self.matrix_kwartieren = matrix
                self.last_build = nu

                if not self.prijs_dict and not self.forecast_dict:
                    status = "degraded"
                elif fallback_slots > 0 and laatste_prijs_slot is None:
                    status = "degraded"
                else:
                    status = "ok"

                n_alle, n_werkdag, n_weekend = self._tel_historie_blokken()
                einde_slot = eerste_slot + datetime.timedelta(minutes=aantal_slots * self.QUARTER_MINUTES)
                kwartier = datetime.timedelta(minutes=self.QUARTER_MINUTES)
                attributes = {
                    "friendly_name": "Energie Core Forecast",
                    "icon": "mdi:chart-timeline-variant",
                    "device_class": "timestamp",
                    "schema_versie": self.SCHEMA_VERSIE,
                    "status": status,
                    "waarschuwingen": waarschuwingen,
                    "generated_at": nu.isoformat(),
                    "horizon_start": eerste_slot.isoformat(),
                    "horizon_einde": einde_slot.isoformat(),
                    "kwartier_minuten": self.QUARTER_MINUTES,
                    "slot_count": len(matrix),
                    "fallback_prijs_slots": fallback_slots,
                    "prijs_dekking_tot": (laatste_prijs_slot + kwartier).isoformat() if laatste_prijs_slot else None,
                    "pv_dekking_tot": (laatste_pv_slot + kwartier).isoformat() if laatste_pv_slot else None,
                    "verbruik_bron": self.verbruik_huis_entity,
                    "verbruik_eenheid": self._verbruik_eenheid_gebruikt,
                    "historie_blokken": n_alle,
                    "historie_profielen": {"werkdag": n_werkdag, "weekend": n_weekend},
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