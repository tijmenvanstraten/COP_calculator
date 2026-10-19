"""Woning Thermische Analyse (AppDaemon) - kwartier-model voor het EMS.

Doel
----
Leert elke nacht (04:30) de thermische eigenschappen van het huis en publiceert die als
sensoren voor emsverwarming.py. Alles werkt op een raster van 15 minuten (EPEX-kwartieren).

Model (per kwartier, dTin/dt in K/h)
------------------------------------
  dTin/dt = (a + c*wind) * (Tout - Tin)         verlies (a = UA/C in 1/h, c per km/h)
          + b  * PV_kW                          zonnewinst (K/h per kW PV)
          + g_snel * P_wp + g_traag * P_wp_ema  warmtepomp (K/h per kW_elektrisch; vloer-traagheid)
          + g_t * P_wp_ema * (Tout - 7) / 10    COP-afhankelijkheid van buitentemperatuur
          + e0 + e1 * dag                       interne winst (nacht / 07-23 uur)

Alle parameters worden tegelijk gefit (gewogen kleinste kwadraten, zonder numpy), zodat
zon, wind, interne winst en vloerinertie elkaar niet meer vervuilen.

Databronnen
-----------
* Numerieke sensoren: recorder.get_statistics, 5-minuten gemiddelden -> kwartieren
  (zelfde methode als energiecorepredictor.py). Geen database-wachtwoord meer nodig.
* WP-status (tekst): get_history, tijdgewogen aandeel per kwartier (verwarmen / uitgesloten).
* Long-term statistics (uurgemiddelden, jaren): "passieve" fit (a, b, c, e) op uren waarin
  de WP uit staat; dient als controle/terugval voor de korte fit.

Vloeropbouw
-----------
Houten balkenvloer met fermacell + vloerverwarming (lage massa, snelle respons) en deels
fermacell op beton (meer massa, trager). Daarom bestaat het WP-effect uit een snelle en een
trage component; de tijdconstante van de trage component wordt elke nacht gekozen uit
TAU_KANDIDATEN_UUR (kleinste residu), tenzij je `tau_traag_uur` vastzet in apps.yaml.

Optioneel: configureer `entities: wp_thermisch_vermogen: sensor.<thermisch vermogen in kW>`
(bijv. uit de COP-calculator). Het model fit dan op warmteafgifte i.p.v. elektrisch vermogen,
laat de COP-correctie vallen en publiceert warmtecapaciteit (kWh/K) en UA (W/K).

Kwaliteitsbewaking
------------------
Een nieuwe fit wordt alleen gebruikt als hij genoeg rijen heeft, fysisch plausibel is
(tekens, tijdconstante) en op een hold-out stuk data (6 uur vooruit simuleren) beter is dan
"temperatuur blijft gelijk". Daarna wordt hij geexponentieel gemiddeld met de vorige waarde.
Waarden staan in een JSON-bestand en worden bij elke start en elk kwartier opnieuw
gepubliceerd (set_state-sensoren verdwijnen anders bij een HA-herstart).
"""

import datetime
import json
import math
import os
import threading
import time
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass


class WoningThermischeAnalyse(hass.Hass):

    # ---------------------------------------------------------------- configuratie
    STANDAARD_ENTITEITEN = {
        "binnen": "sensor.woonkamer_temperatuur_gemiddeld",
        "buiten": "sensor.kantoor_buiten_temperatuur",
        "pv": "sensor.solaredge_i1_ac_power",
        "wind": "sensor.thuis_windsnelheid",
        "wp_status": "sensor.control_unit_operation_state_2",
        "wp_binnen_vermogen": "sensor.shelly_warmtepomp_binnenunit_active_power",
        "wp_buiten_vermogen": "sensor.shelly_warmtepomp_buitenunit_active_power",
        "wp_thermisch_vermogen": "sensor.control_unit_thermal_power_heating",  # thermisch kW; None = uit
    }
    # Eenheden waarnaar recorder.get_statistics omrekent, ongeacht de sensor-eenheid.
    STAT_UNITS = {"temperature": "°C", "power": "kW", "speed": "km/h"}

    WP_VERWARMEN = ["operation_state_heat_thermo_on"]
    WP_IDLE = [
        "operation_state_heat_thermo_off", "operation_state_heat_demand_off",
        "operation_state_dhw_off", "operation_state_cool_demand_off",
        "operation_state_cool_thermo_off", "off",
    ]
    # Uitgesloten worden alleen toestanden die één van deze tekstdelen bevatten (DHW aan, koelen aan,
    # alarm, defrost). Alle andere toestanden gelden als "niet verwarmend" en blijven in de data.
    WP_UITSLUITEN = ("dhw_on", "swp_on", "cool_thermo_on", "cool_demand_on", "alarm", "defrost", "error")
    WP_ONBEKEND = ("", "unknown", "unavailable", "none")

    # Modelconstanten (voorheen magic numbers)
    TAU_KANDIDATEN_UUR = (1.0, 2.0, 4.0, 8.0)  # trage WP-component (balkenvloer ... beton)
    EMA_WARMUP_UUR = 6.0            # inloop van de trage EMA na start/gat (gelijk voor alle tau's)
    HEAT_AAN_DREMPEL = 0.5          # aandeel "thermo_on" in een kwartier om als verwarmen te tellen
    EXCL_UIT_DREMPEL = 0.5          # aandeel uitgesloten toestand waarboven P_heat = 0
    HEAT_P_MIN = 0.15               # minimaal (kW) om mee te tellen voor typisch WP-vermogen
    STATS_MIN_PUNTEN = 2            # minstens 2 van 3 vijfminutenwaarden per kwartier
    HISTORY_DEKKING_MIN = 0.9       # minimale dekking van WP-status per kwartier
    DAG_START_UUR, DAG_EIND_UUR = 7, 23
    OUTLIER_SIGMA = 3.5
    HOLDOUT_MIN_DAGEN = 5           # hold-out pas bij meer dan zoveel dagen data
    BACKTEST_STAP_KW = 8            # nieuwe simulatie elke 2 uur
    BACKTEST_MIN_N = 5
    MAX_B, MAX_C, MAX_G = 5.0, 0.01, 5.0   # plausibiliteitsgrenzen: zon K/h/kW, wind, K/h/kW_wp
    MAX_MAE_RATIO_VORIG = 1.25      # nieuwe fit mag max 25% slechter zijn dan vorige op hold-out

    TAU_MIN_UUR, TAU_MAX_UUR = 0.25, 48.0   # geldig bereik voor tau_traag_uur in apps.yaml
    THERMISCH_W_DREMPEL_KW = 30.0           # p95 boven dit = waarschijnlijk Watt i.p.v. kW
    WP_AAN_THERMISCH_KW = 0.05              # fallback zonder status-history: P > dit = verwarmen
    # Plausibele meetbereiken per grootheid (buiten dit bereik -> waarde genegeerd)
    BEREIK = {
        "binnen": (5.0, 40.0), "buiten": (-30.0, 45.0), "pv": (-0.1, 50.0), "wind": (0.0, 200.0),
        "wp_binnen_vermogen": (-0.1, 30.0), "wp_buiten_vermogen": (-0.1, 30.0),
        "wp_thermisch_vermogen": (-0.1, 60.0),
    }
    MAX_ONGELDIG_AANDEEL = 0.05     # meer dan 5% buiten bereik -> waarschuwing (eenheid?)

    KWARTIER_S = 900
    KWARTIER_H = 0.25
    REF_TOUT = 7.0
    NAMEN_KORT = ["a", "c", "b", "g_snel", "g_traag", "g_t", "e0", "e1"]
    NAMEN_LANG = ["a", "c", "b", "e0", "e1"]
    POSITIEF = {"a", "c", "b", "g_snel", "g_traag"}
    GLAD = ["a", "c", "b", "g_snel", "g_traag", "g_t", "e0", "e1"]

    # ---------------------------------------------------------------- opstarten
    def initialize(self):
        self._lock = threading.Lock()          # voorkomt dat twee analyses tegelijk draaien
        self._opslag_lock = threading.Lock()   # korte lock voor het wisselen/lezen van self.opslag
        self._laad_config()
        self.opslag = self._lees_opslag()      # copy-on-write: wordt nooit in-place gewijzigd
        opgeslagen_tau = self.opslag.get("tau_traag_uur")
        if isinstance(opgeslagen_tau, (int, float)) and self.TAU_MIN_UUR <= opgeslagen_tau <= self.TAU_MAX_UUR:
            self.tau_traag_h = float(opgeslagen_tau)
        self.log("--- Start App Woning Thermische Analyse (kwartier-model) ---")

        self._publiceer()
        self.run_every(self._publiceer_cb, "now+60", self.KWARTIER_S)
        self.run_daily(self.bereken_constantes, "04:30:00")
        self.listen_event(self._handmatig, "woning_thermische_analyse_run")
        self.listen_event(self._publiceer_cb, "plugin_started")

        laatste = self.opslag.get("laatste_run")
        te_oud = True
        if laatste:
            try:
                leeftijd = time.time() - datetime.datetime.fromisoformat(laatste).timestamp()
                te_oud = leeftijd > 20 * 3600
            except ValueError:
                pass
        if te_oud:
            self.run_in(self.bereken_constantes, 90)

    def _arg(self, naam, default, cast=float):
        waarde = self.args.get(naam, default)
        try:
            return cast(waarde)
        except (TypeError, ValueError):
            self.log(f"Ongeldige waarde voor {naam}: {waarde!r}; standaard {default!r}.", level="WARNING")
            return cast(default)

    def _laad_config(self):
        self.ent = dict(self.STANDAARD_ENTITEITEN)
        eigen = self.args.get("entities")
        if isinstance(eigen, dict):
            self.ent.update({k: v for k, v in eigen.items() if k in self.ent and v})
        self.tz = ZoneInfo(str(self.args.get("tijdzone", "Europe/Amsterdam")))
        self.korte_dagen = max(3, self._arg("korte_dagen", 14, int))          # 5-min statistics/history
        self.lange_dagen = max(30, self._arg("lange_dagen", 365, int))         # uurlijkse long-term stats
        self.lang_refresh_dagen = self._arg("lang_refresh_dagen", 7, int)
        self.lag = max(1, self._arg("verschil_kwartieren", 2, int))   # dTin over L kwartieren
        self.tau_traag_h = 3.0
        self.tau_kandidaten = list(self.TAU_KANDIDATEN_UUR)
        tau_arg = self.args.get("tau_traag_uur", "auto")
        if str(tau_arg).lower() != "auto":
            try:
                waarde = float(tau_arg)
            except (TypeError, ValueError):
                waarde = None
            if waarde is not None and self.TAU_MIN_UUR <= waarde <= self.TAU_MAX_UUR:
                self.tau_traag_h, self.tau_kandidaten = waarde, [waarde]
            else:
                self.log(f"tau_traag_uur={tau_arg!r} is ongeldig (toegestaan {self.TAU_MIN_UUR}-"
                         f"{self.TAU_MAX_UUR} uur of 'auto'); 'auto' wordt gebruikt.", level="WARNING")
        self.thermisch_geconfigureerd = bool(self.ent.get("wp_thermisch_vermogen"))
        self.thermisch = self.thermisch_geconfigureerd   # wordt per run gecontroleerd
        self.history_timeout = self._arg("history_timeout_seconden", 120, float)
        self.halfwaarde_dagen = self._arg("halfwaarde_dagen", 7.0, float)
        self.min_delta_t = self._arg("min_delta_t", 3.0, float)
        self.min_rijen = max(50, self._arg("min_rijen", 100, int))
        # minimaal aantal kwartieren (rijen) met actief verwarmen voor de volledige fit; anders passieve fit
        self.min_verwarm_rijen = max(10, self._arg("min_verwarm_rijen", 60, int))
        self.min_thermische_rijen = max(
            self.min_rijen, self._arg("min_thermische_rijen", 300, int)
        )
        self.min_rijen_lang = self._arg("min_rijen_lang", 200, int)
        self.min_r2 = self._arg("min_r2", 0.15, float)
        self.max_rel_se_a = self._arg("max_rel_se_a", 0.35, float)
        self.tau_huis_bereik = (self._arg("tau_huis_min_uur", 10.0, float),
                                self._arg("tau_huis_max_uur", 400.0, float))
        self.max_mae = self._arg("max_mae_6u", 0.6, float)
        self.horizon_kw = self._arg("backtest_horizon_kwartieren", 24, int)
        self.holdout_dagen = max(0.0, self._arg("holdout_dagen", 2.0, float))
        self.smoothing = min(1.0, max(0.05, self._arg("smoothing", 0.3, float)))
        self.excl_drempel = self._arg("uitgesloten_drempel", 0.05, float)
        self.wp_uit_kw = self._arg("wp_uit_vermogen_kw", 0.10, float)
        self.nasleep_uren = self._arg("nasleep_uren", 4, int)
        self.max_tout_lang = self._arg("max_buiten_lang", 17.0, float)
        self.pv_missend_is_nul = bool(self.args.get("pv_missend_is_nul", True))
        self.wp_heat = list(self.args.get("wp_verwarmen_states", self.WP_VERWARMEN))
        self.wp_uitsluiten = [str(x).lower() for x in self.args.get("wp_uitsluit_states", self.WP_UITSLUITEN)]
        self.data_dir = self.args.get("data_dir") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "woning_thermische_analyse_data")
        self.opslag_bestand = os.path.join(self.data_dir, "woning_thermische_analyse.json")
        self.stats_pauze = self._arg("stats_pauze_seconden", 0.05, float)
        self.stats_timeout = self._arg("stats_timeout_seconden", 60, float)

    # ---------------------------------------------------------------- opslag
    def _lees_opslag(self):
        try:
            with open(self.opslag_bestand, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("coef", {})
                return data
        except (OSError, ValueError):
            pass
        return {"versie": 1, "coef": {}, "historie": []}

    def _schrijf_opslag(self):
        tmp = self.opslag_bestand + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.opslag, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.opslag_bestand)
        except OSError as err:
            self.log(f"Opslaan mislukt: {err}", level="ERROR")

    # ---------------------------------------------------------------- publiceren
    def _publiceer_cb(self, *_args, **_kwargs):
        self._publiceer()

    def _zet_sensor(self, entity: str, state, attributes: dict) -> bool:
        """set_state met een korte herpoging; de kwartier-republish is de tweede vangnet."""
        for poging in range(2):
            try:
                self.set_state(entity, state=state, attributes=attributes)
                return True
            except Exception as err:  # noqa: BLE001
                if poging == 0:
                    time.sleep(1.0)
                else:
                    self.log(f"set_state {entity} mislukt: {err!r}", level="WARNING")
        return False

    def _publiceer(self) -> None:
        """Publiceert alle sensoren vanuit de laatst opgeslagen (copy-on-write) toestand."""
        with self._opslag_lock:
            opslag = self.opslag
        c = opslag.get("coef") or {}
        if not c:
            return
        meta = opslag.get("meta", {})
        basis = {
            "datapunten": meta.get("n"),
            "bron": meta.get("bron"),
            "laatste_update": opslag.get("laatste_run"),
            "model": "kwartier_regressie_v2",
        }
        g0 = c.get("g_snel", 0.0) + c.get("g_traag", 0.0)
        e_gem = c.get("e0", 0.0) + c.get("e1", 0.0) * 16.0 / 24.0
        a = c.get("a", 0.0)
        gain_attr = {
            "unit_of_measurement": "K/h/kW",
            "friendly_name": "Huis opwarming per kW (bij 7 C)",
            "invoer": meta.get("invoer"),
            "g_snel": round(c.get("g_snel", 0.0), 5), "g_traag": round(c.get("g_traag", 0.0), 5),
            "aandeel_snel": round(c.get("g_snel", 0.0) / g0, 2) if g0 > 0 else None,
            "tout_helling_per_10k": round(c.get("g_t", 0.0), 5), "ref_buiten": self.REF_TOUT,
            "tau_traag_uur": meta.get("tau_traag_uur"),
            "warmtecapaciteit_kwh_per_k": meta.get("c_kwh_k"), "ua_w_per_k": meta.get("ua_w_k"),
        }
        sensoren = [
            ("sensor.huis_afkoel_constante", c.get("a"), {
                "unit_of_measurement": "1/h", "friendly_name": "Huis afkoelconstante (UA/C)",
                "tijdconstante_uur": round(1.0 / a, 1) if a > 0 else None}),
            ("sensor.huis_solar_gain_constante", c.get("b"), {
                "unit_of_measurement": "K/h/kW", "friendly_name": "Huis zonnewinst per kW PV"}),
            ("sensor.huis_wind_loss_factor", c.get("c"), {
                "unit_of_measurement": "1/h per km/h", "friendly_name": "Huis extra verlies per km/h wind"}),
            ("sensor.huis_opwarm_constante", meta.get("opwarm"), {
                "unit_of_measurement": "K/h", "friendly_name": "Huis bruto opwarmsnelheid (WP aan, 7 C)",
                "typisch_vermogen_kw": meta.get("p_typ")}),
            ("sensor.huis_interne_winst", e_gem, {
                "unit_of_measurement": "K/h", "friendly_name": "Huis interne winst (gemiddeld)",
                "nacht": round(c.get("e0", 0.0), 5), "dag_extra_07_23": round(c.get("e1", 0.0), 5)}),
            ("sensor.huis_verwarming_gain", g0 if "g_snel" in c else None, gain_attr),
        ]
        for entity, waarde, extra in sensoren:
            if waarde is not None:
                self._zet_sensor(entity, round(float(waarde), 5), {**basis, **extra})
        kw = opslag.get("kwaliteit")
        if kw:
            self._zet_sensor("sensor.huis_model_kwaliteit", kw.get("status", "unknown"),
                             {"friendly_name": "Huis thermisch model kwaliteit", **kw})

    # ---------------------------------------------------------------- triggers
    def _handmatig(self, *_args, **_kwargs):
        self.run_in(self.bereken_constantes, 1)

    def bereken_constantes(self, kwargs):
        if not self._lock.acquire(blocking=False):
            self.log("Analyse draait al; overgeslagen.", level="WARNING")
            return
        try:
            self._analyse()
        except Exception as err:  # noqa: BLE001
            self.log(f"Fout tijdens thermische analyse: {err!r}", level="ERROR")
        finally:
            self._lock.release()

    # ================================================================ DATA OPHALEN
    @staticmethod
    def _stat_tijd(waarde):
        if waarde is None:
            return None
        if isinstance(waarde, (int, float)):
            sec = waarde / 1000.0 if waarde > 1e11 else float(waarde)
            return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc)
        if isinstance(waarde, datetime.datetime):
            dt = waarde
        else:
            tekst = str(waarde).strip()
            if tekst.endswith("Z"):
                tekst = tekst[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(tekst)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    @staticmethod
    def _pak_rijen(res, entity_id):
        if not isinstance(res, dict) or res.get("success") is False or res.get("ad_status") not in (None, "OK"):
            return []
        result = res.get("result")
        resp = result.get("response") if isinstance(result, dict) and "response" in result else result
        if isinstance(resp, dict) and "statistics" in resp:
            resp = resp["statistics"]
        rijen = resp.get(entity_id) if isinstance(resp, dict) else None
        return [r for r in rijen if isinstance(r, dict)] if isinstance(rijen, list) else []

    def _haal_stats(self, entity_id, start, einde, period, chunk_dagen):
        """Gemiddelden per statistiekperiode: {start_datetime_utc: mean}."""
        uit = {}
        stap = datetime.timedelta(days=chunk_dagen)
        cursor = start
        while cursor < einde:
            stop = min(cursor + stap, einde)
            try:
                res = self.call_service(
                    "recorder/get_statistics", statistic_ids=[entity_id],
                    start_time=cursor.isoformat(), end_time=stop.isoformat(),
                    period=period, types=["mean"], units=self.STAT_UNITS,
                    hass_timeout=self.stats_timeout, timeout=self.stats_timeout + 10,
                    suppress_log_messages=True)
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(f"recorder/get_statistics mislukt voor {entity_id}: {err!r}") from err
            if isinstance(res, dict) and (res.get("success") is False or res.get("ad_status") not in (None, "OK")):
                self.log(f"get_statistics {entity_id}: onverwacht antwoord "
                         f"(ad_status={res.get('ad_status')!r}, success={res.get('success')!r})", level="WARNING")
            for rij in self._pak_rijen(res, entity_id):
                try:
                    t = self._stat_tijd(rij.get("start"))
                    v = rij.get("mean")
                    if t is not None and v is not None and math.isfinite(float(v)):
                        uit[t] = float(v)
                except (TypeError, ValueError, AttributeError):
                    continue
            cursor = stop
            time.sleep(self.stats_pauze)
        return uit

    @staticmethod
    def _naar_grid(ruw, stap_s, idx0, n, min_n):
        som = [0.0] * n
        tel = [0] * n
        for t, v in ruw.items():
            i = int(t.timestamp() // stap_s) - idx0
            if 0 <= i < n:
                som[i] += v
                tel[i] += 1
        return [som[i] / tel[i] if tel[i] >= min_n else None for i in range(n)]

    def _filter_bereik(self, sleutel: str, reeks: list) -> list:
        """Zet waarden buiten het plausibele bereik op None; kleine negatieve vermogens worden 0."""
        lo, hi = self.BEREIK[sleutel]
        totaal = ongeldig = 0
        uit = []
        for v in reeks:
            if v is None:
                uit.append(None)
                continue
            totaal += 1
            if lo <= v <= hi:
                uit.append(max(v, 0.0) if sleutel in ("pv", "wp_binnen_vermogen", "wp_buiten_vermogen",
                                                       "wp_thermisch_vermogen") else v)
            else:
                ongeldig += 1
                uit.append(None)
        if totaal and ongeldig / totaal > self.MAX_ONGELDIG_AANDEEL:
            self.log(f"{self.ent[sleutel]}: {ongeldig}/{totaal} waarden buiten {lo}..{hi}; "
                     f"controleer eenheid/sensor.", level="WARNING")
        return uit

    def _haal_reeks(self, sleutel: str, start, einde, idx0: int, n: int, period: str,
                    stap_s: int, min_n: int, chunk_dagen: int, verplicht: bool = True) -> list:
        """Statistics -> raster, met eenheidscontrole en plausibiliteitsfilter."""
        entity = self.ent[sleutel]
        ruw = self._haal_stats(entity, start, einde, period, chunk_dagen)
        reeks = self._naar_grid(ruw, stap_s, idx0, n, min_n)
        if sleutel == "wp_thermisch_vermogen":
            waarden = sorted(v for v in reeks if v is not None)
            if waarden and waarden[int(0.95 * (len(waarden) - 1))] > self.THERMISCH_W_DREMPEL_KW:
                self.log(f"{entity}: waarden lijken Watt; omgerekend naar kW.", level="WARNING")
                reeks = [v / 1000.0 if v is not None else None for v in reeks]
        reeks = self._filter_bereik(sleutel, reeks)
        if verplicht and not any(v is not None for v in reeks):
            raise ValueError(
                f"Geen bruikbare statistics voor {entity}: bestaat de entiteit, heeft hij een "
                f"state_class (measurement) en staat hij niet in de recorder-exclude?")
        return reeks

    def _controleer_entiteiten(self) -> None:
        """Pre-flight: bestaan alle sensoren? Thermische sensor ontbreekt -> terug naar elektrisch."""
        self.thermisch = self.thermisch_geconfigureerd
        if self.thermisch and not self.entity_exists(self.ent["wp_thermisch_vermogen"]):
            self.log(f"{self.ent['wp_thermisch_vermogen']} bestaat niet; analyse gebruikt "
                     f"elektrisch vermogen.", level="WARNING")
            self.thermisch = False
        vereist = ["binnen", "buiten", "pv", "wind", "wp_status"]
        if not self.thermisch:
            vereist += ["wp_binnen_vermogen", "wp_buiten_vermogen"]
        ontbrekend = [self.ent[k] for k in vereist if not self.entity_exists(self.ent[k])]
        if ontbrekend:
            raise RuntimeError("Entiteiten bestaan niet in HA: " + ", ".join(ontbrekend))

    def _classificeer(self, state):
        if state is None or str(state).strip().lower() in self.WP_ONBEKEND:
            return None
        if state in self.wp_heat:
            return "heat"
        laag = str(state).lower()
        if any(deel in laag for deel in self.wp_uitsluiten):
            return "excl"
        return "idle"

    def _wp_fracties(self, start, einde, idx0, n):
        """Tijdgewogen aandeel per kwartier: (heat, excl) of None bij onvoldoende dekking."""
        self._status_verdeling = {}
        kwargs = {"entity_id": self.ent["wp_status"], "start_time": start, "end_time": einde}
        try:
            res = self.get_history(timeout=self.history_timeout, **kwargs)
        except TypeError:   # AppDaemon-versie zonder timeout-argument
            res = self.get_history(**kwargs)
        items = res[0] if isinstance(res, list) and res and isinstance(res[0], list) else res
        events = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            t = self._stat_tijd(it.get("last_changed") or it.get("last_updated"))
            if t is not None:
                events.append((t.timestamp(), it.get("state")))
        events.sort(key=lambda e: e[0])
        if not events:
            return None
        begin, eind = start.timestamp(), einde.timestamp()
        acc = {"heat": [0.0] * n, "excl": [0.0] * n, "dek": [0.0] * n}
        seconden = {}
        for (t0, s0), t1 in zip(events, [e[0] for e in events[1:]] + [eind]):
            cls = self._classificeer(s0)
            duur = min(t1, eind) - max(t0, begin)
            if duur > 0:
                seconden[str(s0)] = seconden.get(str(s0), 0.0) + duur
            t, stop = max(t0, begin), min(t1, eind)
            while cls and t < stop:
                q = int(t // self.KWARTIER_S)
                seg = min(stop, (q + 1) * self.KWARTIER_S) - t
                i = q - idx0
                if 0 <= i < n:
                    acc["dek"][i] += seg
                    if cls == "heat":
                        acc["heat"][i] += seg
                    elif cls == "excl":
                        acc["excl"][i] += seg
                t += seg
        totaal = sum(seconden.values()) or 1.0
        top = sorted(seconden.items(), key=lambda kv: -kv[1])[:10]
        self._status_verdeling = {s_: f"{100.0 * v / totaal:.1f}% ({self._classificeer(s_) or 'onbekend'})"
                                  for s_, v in top}
        self.log("WP-status verdeling: " + ", ".join(f"{k}={v}" for k, v in self._status_verdeling.items()))
        heat, excl = [], []
        for i in range(n):
            if acc["dek"][i] >= self.HISTORY_DEKKING_MIN * self.KWARTIER_S:
                heat.append(acc["heat"][i] / acc["dek"][i])
                excl.append(acc["excl"][i] / acc["dek"][i])
            else:
                heat.append(None)
                excl.append(None)
        return heat, excl

    def _bouw_serie(self, start, einde) -> dict:
        """Kwartierraster met alle grootheden voor het korte (hoofd)model."""
        idx0 = int(start.timestamp() // self.KWARTIER_S)
        n = int(einde.timestamp() // self.KWARTIER_S) - idx0
        serie = {"idx0": idx0, "n": n}

        def haal(sleutel, verplicht=True):
            return self._haal_reeks(sleutel, start, einde, idx0, n, "5minute", self.KWARTIER_S,
                                    self.STATS_MIN_PUNTEN, 10, verplicht)

        for sleutel in ("binnen", "buiten", "pv", "wind"):
            serie[sleutel] = haal(sleutel)
        if self.thermisch:
            th = haal("wp_thermisch_vermogen", verplicht=False)
            if sum(v is not None for v in th) < self.min_thermische_rijen:
                self.log(f"Te weinig statistics voor {self.ent['wp_thermisch_vermogen']}; "
                         f"terugval op elektrisch vermogen.", level="WARNING")
                self.thermisch = False
            else:
                serie["pt"] = th
        if not self.thermisch:
            serie["wp_binnen_vermogen"] = haal("wp_binnen_vermogen")
            serie["wp_buiten_vermogen"] = haal("wp_buiten_vermogen")
            serie["pt"] = [(a + b) if a is not None and b is not None else None
                           for a, b in zip(serie["wp_binnen_vermogen"], serie["wp_buiten_vermogen"])]
        try:
            fr = self._wp_fracties(start, einde, idx0, n)
        except Exception as err:  # noqa: BLE001
            self.log(f"get_history {self.ent['wp_status']} mislukt: {err!r}", level="WARNING")
            fr = None
        if fr is None:
            if not self.thermisch:
                raise RuntimeError(f"Geen history voor {self.ent['wp_status']}; analyse afgebroken.")
            self.log("Geen WP-status-history; verwarmen afgeleid uit thermisch vermogen, DHW-uitsluiting "
                     "niet mogelijk.", level="WARNING")
            heat = [None if p is None else (1.0 if p > self.WP_AAN_THERMISCH_KW else 0.0) for p in serie["pt"]]
            excl = [None if p is None else 0.0 for p in serie["pt"]]
            fr = (heat, excl)
        serie["heat"], serie["excl"] = fr
        return serie  # nog niet "geprepareerd": tau wordt per kandidaat gekozen in _korte_fit

    # ================================================================ VOORBEREIDING
    def _dag_vlag(self, idx, stap_s):
        uur = datetime.datetime.fromtimestamp(idx * stap_s, self.tz).hour
        return 1.0 if self.DAG_START_UUR <= uur < self.DAG_EIND_UUR else 0.0

    def _prepareer(self, s: dict, stap_s: int, tau_h: float, kort: bool, warm_uur: float = None) -> dict:
        """Afgeleide reeksen: P_heat, trage EMA, dag-vlag, PV-invulling."""
        n, idx0 = s["n"], s["idx0"]
        stap_h = stap_s / 3600.0
        tau_h = max(float(tau_h), stap_h)   # nooit <= 0: voorkomt deling door nul in de EMA
        if self.pv_missend_is_nul:
            s["pv"] = [0.0 if (v is None and s["buiten"][i] is not None) else v
                       for i, v in enumerate(s["pv"])]
        s["dag"] = [self._dag_vlag(idx0 + i, stap_s) for i in range(n)]
        if kort:
            s["pheat"] = [
                None if (p is None or x is None) else (p if x < self.EXCL_UIT_DREMPEL else 0.0)
                for p, x in zip(s["pt"], s["excl"])]
        else:
            s["pheat"] = list(s["pt"])
        alpha = 1.0 - math.exp(-stap_h / tau_h)
        warm = max(2, int(math.ceil((warm_uur if warm_uur is not None else tau_h) / stap_h)))
        ema, run = None, 0
        s["ema"], s["ema_ok"] = [], []
        for p in s["pheat"]:
            if p is None:
                run = 0
                s["ema"].append(ema)
                s["ema_ok"].append(False)
                continue
            ema = p if ema is None else ema + alpha * (p - ema)
            run += 1
            s["ema"].append(ema)
            s["ema_ok"].append(run >= warm)
        return s

    # ================================================================ REGRESSIE
    @staticmethod
    def _inverteer(A):
        n = len(A)
        M = [list(r) + [1.0 if i == j else 0.0 for j in range(n)] for i, r in enumerate(A)]
        for c in range(n):
            p = max(range(c, n), key=lambda r: abs(M[r][c]))
            if abs(M[p][c]) < 1e-14:
                return None
            M[c], M[p] = M[p], M[c]
            piv = M[c][c]
            M[c] = [v / piv for v in M[c]]
            for r in range(n):
                if r != c and M[r][c] != 0.0:
                    f = M[r][c]
                    M[r] = [v - f * cv for v, cv in zip(M[r], M[c])]
        return [r[n:] for r in M]

    def _wls(self, X, y, w):
        k = len(X[0])
        A = [[0.0] * k for _ in range(k)]
        b = [0.0] * k
        for xi, yi, wi in zip(X, y, w):
            for r in range(k):
                wx = wi * xi[r]
                b[r] += wx * yi
                for c in range(r, k):
                    A[r][c] += wx * xi[c]
        for r in range(k):
            for c in range(r):
                A[r][c] = A[c][r]
            A[r][r] += 1e-9 * A[r][r] + 1e-9
        inv = self._inverteer(A)
        if inv is None:
            return None, None
        coef = [sum(inv[r][c] * b[c] for c in range(k)) for r in range(k)]
        return coef, inv

    def _fit(self, rijen, alle_namen, gebruik, positief):
        """Gewogen LS met sign-constraints (kolommen met verkeerd teken vallen af) + 1 outlier-ronde."""
        idx = {n: i for i, n in enumerate(alle_namen)}
        gebruik = list(gebruik)
        res = None
        for ronde in range(2):
            while True:
                cols = [idx[n] for n in gebruik]
                X = [[r[0][j] for j in cols] for r in rijen]
                y = [r[1] for r in rijen]
                w = [r[2] for r in rijen]
                coef, inv = self._wls(X, y, w)
                if coef is None:
                    return None
                slecht = [(coef[i], n) for i, n in enumerate(gebruik) if n in positief and coef[i] < 0]
                if not slecht:
                    break
                gebruik.remove(min(slecht)[1])
                if not gebruik:
                    return None
            resid = [yi - sum(c * xv for c, xv in zip(coef, xi)) for xi, yi in zip(X, y)]
            sw = sum(w)
            gem = sum(wi * yi for wi, yi in zip(w, y)) / sw
            ss_res = sum(wi * r * r for wi, r in zip(w, resid))
            ss_tot = sum(wi * (yi - gem) ** 2 for wi, yi in zip(w, y))
            std = math.sqrt(ss_res / sw)
            n_eff = len(rijen)
            sigma2 = ss_res / max(1.0, sw) * (sw / max(1, n_eff - len(gebruik))) if n_eff > len(gebruik) else 0.0
            res = {
                "n": n_eff, "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0, "resid_std": std,
                "coef": {n: 0.0 for n in alle_namen}, "se": {},
            }
            for i, n in enumerate(gebruik):
                res["coef"][n] = coef[i]
                res["se"][n] = math.sqrt(max(0.0, sigma2 * inv[i][i]))
            if ronde == 0 and std > 0:
                behoud = [r for r, e in zip(rijen, resid) if abs(e) <= self.OUTLIER_SIGMA * std]
                if len(behoud) == len(rijen):
                    break
                rijen = behoud
            else:
                break
        return res

    def _fit_model(self, rijen, alle_namen, tout_bereik=None, uitsluiten=()):
        gebruik = [n for n in alle_namen if n not in uitsluiten]
        for _ in range(2):
            res = self._fit(rijen, alle_namen, gebruik, self.POSITIEF)
            if res is None or "g_t" not in gebruik or tout_bereik is None:
                return res
            c = res["coef"]
            g0 = c["g_snel"] + c["g_traag"]
            lo, hi = tout_bereik
            gt_ok = g0 > 0 and min(g0 + c["g_t"] * (lo - self.REF_TOUT) / 10,
                                   g0 + c["g_t"] * (hi - self.REF_TOUT) / 10) > 0
            if gt_ok:
                return res
            gebruik.remove("g_t")
        return res

    # ---------------------------------------------------------------- rijen kort model
    def _rijen_kort(self, s, i_van=0, i_tot=None, passief=False):
        L = self.lag
        wk = [0.5] + [1.0] * (L - 1) + [0.5]
        wk = [v / L for v in wk]
        n, idx0 = s["n"], s["idx0"]
        i_tot = n - L if i_tot is None else min(i_tot, n - L)
        rijen, touts, heat_p = [], [], []
        afwijzingen = {
            "ontbrekende_binnentemperatuur": 0,
            "ontbrekende_buitentemperatuur": 0,
            "ontbrekende_pv": 0,
            "ontbrekende_wind": 0,
            "ontbrekend_wp_vermogen": 0,
            "onvoldoende_ema_opwarming": 0,
            "ontbrekende_wp_status": 0,
            "uitgesloten_warmtepompstatus": 0,
            "wp_actief_of_nasleep": 0,
            "klein_temperatuurverschil": 0,
        }
        for i in range(i_van, i_tot):
            ks = range(i, i + L + 1)
            if s["binnen"][i] is None or s["binnen"][i + L] is None:
                afwijzingen["ontbrekende_binnentemperatuur"] += 1
                continue
            afwijsreden = None
            for k in ks:
                ontbrekende_data = (
                    ("ontbrekende_binnentemperatuur", s["binnen"][k]),
                    ("ontbrekende_buitentemperatuur", s["buiten"][k]),
                    ("ontbrekende_pv", s["pv"][k]),
                    ("ontbrekende_wind", s["wind"][k]),
                    ("ontbrekend_wp_vermogen", s["pheat"][k]),
                    ("ontbrekende_wp_status", s["excl"][k]),
                )
                afwijsreden = next(
                    (reden for reden, waarde in ontbrekende_data if waarde is None),
                    None,
                )
                if afwijsreden:
                    break
                if not s["ema_ok"][k]:
                    afwijsreden = "onvoldoende_ema_opwarming"
                    break
                if s["excl"][k] > self.excl_drempel:
                    afwijsreden = "uitgesloten_warmtepompstatus"
                    break
                if passief and (s["pheat"][k] > self.wp_uit_kw or s["ema"][k] > self.wp_uit_kw):
                    afwijsreden = "wp_actief_of_nasleep"   # passieve fit: alleen rijen zonder WP-invloed
                    break
            if afwijsreden:
                afwijzingen[afwijsreden] += 1
                continue
            m = lambda key: sum(w * s[key][k] for w, k in zip(wk, ks))  # noqa: E731
            tin, tout, wind, pv = m("binnen"), m("buiten"), m("wind"), m("pv")
            pf, ps, dag = m("pheat"), m("ema"), m("dag")
            dt = tout - tin
            if abs(dt) < self.min_delta_t:
                afwijzingen["klein_temperatuurverschil"] += 1
                continue
            y = (s["binnen"][i + L] - s["binnen"][i]) / (L * self.KWARTIER_H)
            x = [dt, wind * dt, pv, pf, ps, ps * (tout - self.REF_TOUT) / 10.0, 1.0, dag]
            leeftijd_d = (n - i) * self.KWARTIER_H / 24.0
            w = 0.5 ** (leeftijd_d / self.halfwaarde_dagen) if self.halfwaarde_dagen > 0 else 1.0
            rijen.append((x, y, w))
            touts.append(tout)
            if pf > self.HEAT_P_MIN:
                heat_p.append(pf)
        return rijen, touts, heat_p, afwijzingen

    # ---------------------------------------------------------------- simulatie / backtest
    def _stap_model(self, c, tin, tout, wind, pv, pf, ps, dag):
        d = ((c["a"] + c["c"] * wind) * (tout - tin) + c["b"] * pv + c["g_snel"] * pf
             + c["g_traag"] * ps + c["g_t"] * ps * (tout - self.REF_TOUT) / 10.0
             + c["e0"] + c["e1"] * dag)
        return tin + d * self.KWARTIER_H

    def _backtest(self, s, c, vanaf, passief=False):
        """6 uur vooruit simuleren met werkelijke weer- en WP-invoer (open loop)."""
        H = self.horizon_kw
        fouten, basis = [], []
        n = s["n"]
        for i in range(max(vanaf, 0), n - H, self.BACKTEST_STAP_KW):
            t = s["binnen"][i]
            if t is None or s["binnen"][i + H] is None:
                continue
            ok = True
            for k in range(i, i + H):
                if (s["buiten"][k] is None or s["pv"][k] is None or s["wind"][k] is None
                        or s["pheat"][k] is None or not s["ema_ok"][k]
                        or (passief and (s["pheat"][k] > self.wp_uit_kw or s["ema"][k] > self.wp_uit_kw))):
                    ok = False
                    break
            if not ok:
                continue
            for k in range(i, i + H):
                t = self._stap_model(c, t, s["buiten"][k], s["wind"][k], s["pv"][k],
                                     s["pheat"][k], s["ema"][k], s["dag"][k])
            fouten.append(abs(t - s["binnen"][i + H]))
            basis.append(abs(s["binnen"][i] - s["binnen"][i + H]))
        if len(fouten) < self.BACKTEST_MIN_N:
            return None
        return {"mae": sum(fouten) / len(fouten), "mae_persistentie": sum(basis) / len(basis),
                "n": len(fouten)}

    # ================================================================ KORTE FIT
    def _korte_fit(self, nu: datetime.datetime):
        """Kwartier-fit op de laatste `korte_dagen`; geeft (fit of None, kwaliteitsdict)."""
        einde = datetime.datetime.fromtimestamp(
            (int(nu.timestamp() // self.KWARTIER_S)) * self.KWARTIER_S, datetime.timezone.utc)
        start = einde - datetime.timedelta(days=self.korte_dagen)
        s = self._bouw_serie(start, einde)
        uitsluiten = ("g_t",) if self.thermisch else ()
        heeft_holdout = s["n"] > self.HOLDOUT_MIN_DAGEN * 96
        cut = s["n"] - int(self.holdout_dagen * 96) if heeft_holdout else 0

        def tout_bereik(touts):
            if len(touts) < 10:
                return None
            tt = sorted(touts)
            return tt[int(0.05 * len(tt))], tt[int(0.95 * len(tt)) - 1]

        # 1. kies de tijdconstante van de trage WP-component (lichte vloer vs. beton)
        beste = None
        scores = {}
        beste_afwijzingen = None
        meeste_rijen = 0
        for tau in self.tau_kandidaten:
            self._prepareer(s, self.KWARTIER_S, tau, kort=True, warm_uur=self.EMA_WARMUP_UUR)
            rijen, touts, heat_p, afwijzingen = self._rijen_kort(s)
            if len(rijen) > meeste_rijen:
                meeste_rijen = len(rijen)
                beste_afwijzingen = afwijzingen
            if len(rijen) < self.min_rijen:
                continue
            fit = self._fit_model(rijen, self.NAMEN_KORT, tout_bereik(touts), uitsluiten)
            if fit is None:
                continue
            scores[str(tau)] = round(fit["resid_std"], 4)
            if beste is None or fit["resid_std"] < beste["fit"]["resid_std"]:
                beste = {"tau": tau, "fit": fit, "heat_p": heat_p}
        if beste is None:
            reden = (
                f"te weinig bruikbare rijen ({meeste_rijen} < {self.min_rijen})"
                if meeste_rijen < self.min_rijen
                else f"regressie mislukt met voldoende rijen ({meeste_rijen})"
            )
            return self._passieve_fit(s, reden)
        if len(beste["heat_p"]) < self.min_verwarm_rijen:
            return self._passieve_fit(
                s, f"te weinig verwarmingsrijen ({len(beste['heat_p'])} < {self.min_verwarm_rijen})")
        tau, fit = beste["tau"], beste["fit"]
        self._prepareer(s, self.KWARTIER_S, tau, kort=True, warm_uur=self.EMA_WARMUP_UUR)
        rijen, touts, _, _ = self._rijen_kort(s)

        # 2. backtest: hold-out (fit zonder laatste dagen) of anders in-sample
        bt, bt_type = None, "hold_out"
        if heeft_holdout:
            rijen_tr, touts_tr, _, _ = self._rijen_kort(s, 0, cut)
            if len(rijen_tr) >= self.min_rijen // 2:
                fit_tr = self._fit_model(rijen_tr, self.NAMEN_KORT, tout_bereik(touts_tr), uitsluiten)
                if fit_tr:
                    bt = self._backtest(s, fit_tr["coef"], cut)
        if bt is None:
            bt, bt_type = self._backtest(s, fit["coef"], 0), "in_sample"
        oud = self.opslag.get("coef") or {}
        invoer_nu = "thermisch" if self.thermisch else "elektrisch"
        bt_oud = None
        # vorig model alleen vergelijken als de WP-eenheid (kW_th vs kW_e) gelijk is
        if bt and all(k in oud for k in self.NAMEN_KORT) and self.opslag.get("meta", {}).get("invoer") == invoer_nu:
            bt_oud = self._backtest(s, oud, cut if bt_type == "hold_out" else 0)

        # 3. fysieke plausibiliteit en kwaliteitspoort
        redenen = []
        c = fit["coef"]
        g0 = c["g_snel"] + c["g_traag"]
        if g0 <= 0:
            return self._passieve_fit(s, "geen positief WP-effect gevonden (g <= 0)")
        tau_huis = 1.0 / c["a"] if c["a"] > 0 else None
        if tau_huis is None or not (self.tau_huis_bereik[0] <= tau_huis <= self.tau_huis_bereik[1]):
            redenen.append(f"tijdconstante huis {tau_huis if tau_huis is None else round(tau_huis, 1)} h buiten bereik")
        se_a = fit["se"].get("a")
        if c["a"] > 0 and se_a is not None and se_a / c["a"] > self.max_rel_se_a:
            redenen.append(f"a onzeker (se/a = {se_a / c['a']:.2f})")
        if g0 <= 0:
            redenen.append("geen positief WP-effect gevonden (g <= 0)")
        if g0 > self.MAX_G or c["b"] > self.MAX_B or c["c"] > self.MAX_C:
            redenen.append("b, c of g buiten plausibel bereik")
        if fit["r2"] < self.min_r2:
            redenen.append(f"R2 {fit['r2']:.2f} < {self.min_r2}")
        if bt is None:
            redenen.append("backtest niet mogelijk (te weinig aaneengesloten data)")
        else:
            if bt["mae"] > bt["mae_persistentie"]:
                redenen.append(f"model slechter dan 'blijft gelijk' ({bt['mae']:.2f} vs {bt['mae_persistentie']:.2f} K)")
            if bt["mae"] > self.max_mae:
                redenen.append(f"MAE {bt['mae']:.2f} K > {self.max_mae} K")
            if bt_oud and bt["mae"] > self.MAX_MAE_RATIO_VORIG * bt_oud["mae"]:
                redenen.append(f"slechter dan vorig model ({bt['mae']:.2f} vs {bt_oud['mae']:.2f} K)")
        hp = sorted(beste["heat_p"])
        fit["p_typ"] = hp[len(hp) // 2] if hp else None
        fit["tau"] = tau
        kw = {
            "n": fit["n"], "r2": round(fit["r2"], 3), "resid_std_Kh": round(fit["resid_std"], 3),
            "a_se": round(se_a, 5) if se_a is not None else None,
            "tau_huis_uur": round(tau_huis, 1) if tau_huis else None,
            "tau_traag_uur": tau, "tau_scores": scores, "invoer": invoer_nu,
            "backtest_type": bt_type, "mae_6u_K": round(bt["mae"], 3) if bt else None,
            "mae_persistentie_K": round(bt["mae_persistentie"], 3) if bt else None,
            "mae_vorig_model_K": round(bt_oud["mae"], 3) if bt_oud else None,
            "backtest_n": bt["n"] if bt else 0,
            "wp_status_verdeling": getattr(self, "_status_verdeling", {}),
            "modus": "volledig", "verwarm_rijen": len(beste["heat_p"]),
            "redenen": redenen, "status": "ok" if not redenen else "degraded",
        }
        return (fit if not redenen else None), kw

    def _passieve_fit(self, s: dict, aanleiding: str):
        """Terugval bij weinig of geen verwarmingsdata (bijv. mild weer): alleen verlies, zon, wind en
        interne winst. Rijen waarin de WP draait of nog nawarmt blijven buiten de fit; de WP-termen
        (g) worden niet geschat en de vorige waarden blijven staan."""
        self._prepareer(s, self.KWARTIER_S, self.tau_traag_h, kort=True, warm_uur=self.EMA_WARMUP_UUR)
        uitsluiten = ("g_snel", "g_traag", "g_t")
        rijen, _touts, _hp, afw = self._rijen_kort(s, passief=True)
        kw = {
            "modus": "passief", "aanleiding": aanleiding,
            "invoer": "thermisch" if self.thermisch else "elektrisch",
            "bruikbare_rijen": len(rijen), "min_rijen": self.min_rijen, "rij_afwijzingen": afw,
            "wp_status_verdeling": getattr(self, "_status_verdeling", {}),
            "status": "degraded", "redenen": [],
        }
        if len(rijen) < self.min_rijen:
            kw["redenen"] = [aanleiding, f"passieve fit: te weinig rijen zonder WP ({len(rijen)} < {self.min_rijen})"]
            return None, kw
        fit = self._fit_model(rijen, self.NAMEN_KORT, None, uitsluiten)
        if fit is None:
            kw["redenen"] = [aanleiding, "passieve regressie mislukt (singulier)"]
            return None, kw
        heeft_holdout = s["n"] > self.HOLDOUT_MIN_DAGEN * 96
        cut = s["n"] - int(self.holdout_dagen * 96) if heeft_holdout else 0
        bt, bt_type = None, "hold_out"
        if heeft_holdout:
            rijen_tr = self._rijen_kort(s, 0, cut, passief=True)[0]
            if len(rijen_tr) >= self.min_rijen // 2:
                fit_tr = self._fit_model(rijen_tr, self.NAMEN_KORT, None, uitsluiten)
                if fit_tr:
                    bt = self._backtest(s, fit_tr["coef"], cut, passief=True)
        if bt is None:
            bt, bt_type = self._backtest(s, fit["coef"], 0, passief=True), "in_sample"
        redenen = []
        c = fit["coef"]
        tau_huis = 1.0 / c["a"] if c["a"] > 0 else None
        if tau_huis is None or not (self.tau_huis_bereik[0] <= tau_huis <= self.tau_huis_bereik[1]):
            redenen.append(f"tijdconstante huis {tau_huis if tau_huis is None else round(tau_huis, 1)} h buiten bereik")
        se_a = fit["se"].get("a")
        if c["a"] > 0 and se_a is not None and se_a / c["a"] > self.max_rel_se_a:
            redenen.append(f"a onzeker (se/a = {se_a / c['a']:.2f})")
        if c["b"] > self.MAX_B or c["c"] > self.MAX_C:
            redenen.append("b of c buiten plausibel bereik")
        if fit["r2"] < self.min_r2:
            redenen.append(f"R2 {fit['r2']:.2f} < {self.min_r2}")
        if bt is None:
            redenen.append("passieve backtest niet mogelijk (te weinig aaneengesloten WP-vrije uren)")
        else:
            if bt["mae"] > bt["mae_persistentie"]:
                redenen.append(f"model slechter dan 'blijft gelijk' ({bt['mae']:.2f} vs {bt['mae_persistentie']:.2f} K)")
            if bt["mae"] > self.max_mae:
                redenen.append(f"MAE {bt['mae']:.2f} K > {self.max_mae} K")
        fit["modus"], fit["tau"], fit["p_typ"] = "passief", self.tau_traag_h, None
        kw.update({
            "n": fit["n"], "r2": round(fit["r2"], 3), "resid_std_Kh": round(fit["resid_std"], 3),
            "a_se": round(se_a, 5) if se_a is not None else None,
            "tau_huis_uur": round(tau_huis, 1) if tau_huis else None,
            "backtest_type": bt_type, "mae_6u_K": round(bt["mae"], 3) if bt else None,
            "mae_persistentie_K": round(bt["mae_persistentie"], 3) if bt else None,
            "backtest_n": bt["n"] if bt else 0,
            "redenen": redenen, "status": "ok" if not redenen else "degraded",
        })
        return (fit if not redenen else None), kw

    # ================================================================ LANGE FIT (long-term)
    def _lange_fit(self, nu):
        """Passieve fit op uurlijkse long-term statistics: alleen uren zonder WP-vermogen."""
        einde = datetime.datetime.fromtimestamp((int(nu.timestamp() // 3600)) * 3600, datetime.timezone.utc)
        start = einde - datetime.timedelta(days=self.lange_dagen)
        idx0 = int(start.timestamp() // 3600)
        n = int(einde.timestamp() // 3600) - idx0
        s = {"idx0": idx0, "n": n}
        for sleutel in ("wp_binnen_vermogen", "wp_buiten_vermogen"):
            if not self.entity_exists(self.ent[sleutel]):
                return None, {"n": 0, "reden": f"{self.ent[sleutel]} bestaat niet (nodig om 'WP uit' te herkennen)"}
        for sleutel in ("binnen", "buiten", "pv", "wind", "wp_binnen_vermogen", "wp_buiten_vermogen"):
            s[sleutel] = self._haal_reeks(sleutel, start, einde, idx0, n, "hour", 3600, 1, 60)
        s["pt"] = [(a + b) if a is not None and b is not None else None
                   for a, b in zip(s["wp_binnen_vermogen"], s["wp_buiten_vermogen"])]
        s = self._prepareer(s, 3600, self.tau_traag_h, kort=False)
        stil, rijen = 0, []
        stil_reeks = []
        for p in s["pt"]:
            stil = stil + 1 if (p is not None and p < self.wp_uit_kw) else 0
            stil_reeks.append(stil)
        for i in range(n - 1):
            k0, k1 = i, i + 1
            if stil_reeks[k1] < self.nasleep_uren + 1:
                continue
            vals = [s[key][k] for k in (k0, k1) for key in ("binnen", "buiten", "pv", "wind")]
            if any(v is None for v in vals):
                continue
            tin = 0.5 * (s["binnen"][k0] + s["binnen"][k1])
            tout = 0.5 * (s["buiten"][k0] + s["buiten"][k1])
            if tout > self.max_tout_lang or abs(tout - tin) < self.min_delta_t:
                continue
            wind = 0.5 * (s["wind"][k0] + s["wind"][k1])
            pv = 0.5 * (s["pv"][k0] + s["pv"][k1])
            dag = 0.5 * (s["dag"][k0] + s["dag"][k1])
            dt = tout - tin
            rijen.append(([dt, wind * dt, pv, 1.0, dag], s["binnen"][k1] - s["binnen"][k0], 1.0))
        if len(rijen) < self.min_rijen_lang:
            return None, {"n": len(rijen), "reden": "te weinig stille uren"}
        fit = self._fit(rijen, self.NAMEN_LANG, self.NAMEN_LANG, self.POSITIEF)
        if fit is None:
            return None, {"n": len(rijen), "reden": "regressie mislukt"}
        a = fit["coef"]["a"]
        tau = 1.0 / a if a > 0 else None
        if tau is None or not (self.tau_huis_bereik[0] <= tau <= self.tau_huis_bereik[1]) or fit["r2"] < 0.1:
            return None, {"n": fit["n"], "r2": round(fit["r2"], 3), "tau_uur": tau and round(tau, 1),
                          "reden": "onbetrouwbaar"}
        return fit, {"n": fit["n"], "r2": round(fit["r2"], 3), "tau_uur": round(tau, 1),
                     "a": round(a, 5), "b": round(fit["coef"]["b"], 4), "c": round(fit["coef"]["c"], 6),
                     "bijgewerkt": nu.isoformat()}

    # ================================================================ ORKESTRATIE
    def _analyse(self) -> None:
        """Korte fit + (wekelijks) lange fit, kwaliteitspoort, EMA, opslaan en publiceren."""
        nu = datetime.datetime.now(datetime.timezone.utc)
        self.log("Thermische analyse gestart.")
        self._controleer_entiteiten()
        kort, kw = self._korte_fit(nu)
        nieuw_opslag = dict(self.opslag)   # copy-on-write: self.opslag zelf wordt niet aangepast

        lang_info = nieuw_opslag.get("lang")
        lang_fit = None
        verouderd = True
        laatste_lang_poging = nieuw_opslag.get("lang_laatste_poging")
        laatste_poging_dt = None
        if isinstance(laatste_lang_poging, dict):
            try:
                laatste_poging_dt = datetime.datetime.fromisoformat(
                    laatste_lang_poging.get("laatste_poging", "")
                )
            except ValueError:
                pass
        laatste_succes_dt = None
        if isinstance(lang_info, dict):
            try:
                laatste_succes_dt = datetime.datetime.fromisoformat(
                    lang_info.get("bijgewerkt", "")
                )
            except ValueError:
                pass
        if laatste_poging_dt and (
            laatste_succes_dt is None or laatste_poging_dt > laatste_succes_dt
        ):
            verouderd = (nu - laatste_poging_dt).days >= self.lang_refresh_dagen
        elif laatste_succes_dt:
            verouderd = (nu - laatste_succes_dt).days >= self.lang_refresh_dagen
        if verouderd:
            try:
                lang_fit, info = self._lange_fit(nu)
                if lang_fit is not None:
                    lang_info = info
                    nieuw_opslag["lang"] = info
                    nieuw_opslag.pop("lang_laatste_poging", None)
                else:
                    info["laatste_poging"] = nu.isoformat()
                    nieuw_opslag["lang_laatste_poging"] = info
            except Exception as err:  # noqa: BLE001
                self.log(f"Long-term fit mislukt: {err!r}", level="WARNING")
                nieuw_opslag["lang_laatste_poging"] = {
                    "laatste_poging": nu.isoformat(),
                    "reden": str(err),
                }

        oud = nieuw_opslag.get("coef") or {}
        oud_meta = nieuw_opslag.get("meta", {})
        if kort is not None and oud_meta.get("invoer") not in (None, kw.get("invoer")):
            # eenheid van de g-termen verandert (kW_e <-> kW_th): niet middelen met oude waarden
            self.log("WP-invoer gewijzigd; g-termen worden niet met oude waarden gemiddeld.", level="WARNING")
            oud = {k: v for k, v in oud.items() if k not in ("g_snel", "g_traag", "g_t")}
        passief = bool(kort is not None and kort.get("modus") == "passief")
        nieuw, bron, n = None, None, 0
        if kort is not None:
            nieuw, bron, n = dict(kort["coef"]), "kort", kort["n"]
            if passief:
                # alleen verlies/zon/wind/interne winst bijwerken; WP-termen blijven zoals ze waren
                bron = "kort_passief"
                nieuw = {k: v for k, v in nieuw.items() if k in ("a", "b", "c", "e0", "e1")}
                nieuw.update({k: oud[k] for k in ("g_snel", "g_traag", "g_t") if k in oud})
            la = (lang_info or {}).get("a")
            if la and abs(nieuw["a"] - la) / la > 0.5:
                kw["afwijking_lang"] = f"a (kort {nieuw['a']:.4f}) wijkt >50% af van long-term ({la:.4f})"
        elif lang_fit is not None and oud:
            # korte fit mislukt: alleen de passieve parameters (a, b, c, e) bijsturen
            nieuw = dict(oud)
            for k in ("a", "b", "c", "e0", "e1"):
                nieuw[k] = lang_fit["coef"][k]
            bron, n = "lang", lang_fit["n"]

        kw["laatste_run"] = nu.isoformat()
        kw["lang"] = lang_info
        nieuw_opslag["kwaliteit"] = kw
        nieuw_opslag["laatste_run"] = nu.isoformat()
        if nieuw is None:
            self.log(
                "Geen bruikbare fit; vorige waarden blijven: "
                f"{kw.get('redenen')}; rijen={kw.get('bruikbare_rijen')}/"
                f"{kw.get('min_rijen')}; afwijzingen={kw.get('rij_afwijzingen')}; "
                f"wp_status={kw.get('wp_status_verdeling')}.",
                level="WARNING",
            )
        else:
            invoer = kw.get("invoer") if kort else oud_meta.get("invoer")
            alpha = self.smoothing if oud else 1.0
            coef = {}
            for k in self.GLAD:
                if k in nieuw:   # g-termen ontbreken zolang er nog geen verwarmingsdata is geweest
                    coef[k] = oud[k] + alpha * (nieuw[k] - oud[k]) if k in oud else nieuw[k]
            heeft_g = "g_snel" in coef and "g_traag" in coef
            g0 = coef["g_snel"] + coef["g_traag"] if heeft_g else None
            p_typ = (kort.get("p_typ") if kort else None) or oud_meta.get("p_typ")
            if kort is None or passief:
                tau_traag = oud_meta.get("tau_traag_uur", self.tau_traag_h)
            else:
                tau_traag = kort["tau"]
            meta = {"bron": bron, "n": n, "tau_traag_uur": tau_traag,
                    "invoer": invoer, "modus": "passief" if passief else "volledig",
                    "p_typ": round(p_typ, 3) if p_typ else None,
                    "opwarm": round(g0 * p_typ, 4) if (g0 and p_typ) else oud_meta.get("opwarm")}
            if invoer == "thermisch" and g0 and g0 > 0:
                meta["c_kwh_k"] = round(1.0 / g0, 2)                      # kWh_th per K
                meta["ua_w_k"] = round(coef["a"] / g0 * 1000.0, 1)        # W/K
            nieuw_opslag["coef"] = coef
            nieuw_opslag["meta"] = meta
            nieuw_opslag["tau_traag_uur"] = tau_traag
            hist = list(nieuw_opslag.get("historie", []))
            hist.append({"t": nu.isoformat(), "bron": bron, "n": n, "tau_traag": tau_traag,
                         **{k: round(v, 5) for k, v in coef.items()}})
            nieuw_opslag["historie"] = hist[-400:]
            self.tau_traag_h = float(tau_traag)
            self.log(
                f"Analyse klaar ({bron}, n={n}): tau_huis={1 / coef['a']:.0f} h, a={coef['a']:.4f}, "
                f"b={coef['b']:.3f} K/h/kW, c={coef['c']:.5f}, "
                f"g={'%.3f K/h/kW' % g0 if g0 is not None else 'nog onbekend (geen verwarmingsdata)'}, "
                f"tau_traag={tau_traag} h, "
                f"e0={coef['e0']:.3f}, e1={coef['e1']:.3f}; MAE6u={kw.get('mae_6u_K')}.")
        with self._opslag_lock:
            self.opslag = nieuw_opslag
        self._schrijf_opslag()
        self._publiceer()