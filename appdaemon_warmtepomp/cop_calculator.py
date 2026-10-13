"""COP-calculator voor een Hitachi Yutaki warmtepomp (AppDaemon).

Berekent de COP voor verwarmen, koelen en warm tapwater (DHW), per periode
(realtime, dag, maand, jaar, lifetime) en publiceert de resultaten als sensoren.

Elektrische energie
-------------------
Het elektrische verbruik komt uit de kWh-tellers van de Shelly's (zonder quantisatie- of
samplingfouten). De gemeten vermogens bepalen alleen hoe de teller-delta over verwarmen,
koelen, DHW, het DHW-element en stand-by wordt verdeeld. Ontbreekt een teller, dan wordt
teruggevallen op integratie van het gemeten vermogen.

Thermische energie
------------------
Verwarmen/koelen: debiet x (uitlaat - inlaat) x 4180 J/(kg*K). DHW: standaard de
tanktemperatuurstijging; daarnaast wordt ook debiet x dT bijgehouden als controle
(``dhw_thermal_method: flow`` gebruikt die, maar bij een ΔT van ~1 K is dat onbetrouwbaar).
``delta_t_offset`` (of ``delta_t_offset_curve``, offset per inlaattemperatuur) corrigeert een
onderlinge afwijking tussen de in- en uitlaatsensor. De app meet die zelf in de uren dat de
compressor uit is en de pomp circuleert (uitlaat = inlaat) en bewaart de metingen.

Temperatuursensoren (temperature_source)
-----------------------------------------
Er zijn twee sensorsets voor uitlaat en inlaat: eigen sensoren (2 decimalen, ``own_*``) en de
ingebouwde (hele graden, ``builtin_*``). ``temperature_source`` kiest: ``own``, ``builtin`` of
``auto`` (eigen sensoren, met de ingebouwde als terugval). Er wordt nooit gemengd: uitlaat en
inlaat komen altijd uit dezelfde set. Optioneel kan een ``input_select`` live wisselen
(``temperature_source_entity``). Elke set heeft een eigen offset.

Warm tapwater: in DHW-modus loopt het water door de driewegklep naar de tankspiraal. Sensoren
buiten de unit (op de verwarmingsleidingen) zien dat circuit niet of slechts deels, daarom
gebruikt DHW standaard de ingebouwde sensoren (``dhw_temperature_source: builtin``).

Controle van de ΔT-sensoren (delta_t_audit)
-------------------------------------------
Tijdens bedrijf wordt de ΔT van beide sets vergeleken. Eén meting van de ingebouwde sensoren is
grof, maar over uren bedrijf middelt de afrondingsfout weg. Het verschil per modus staat in de
statussensor (``delta_t_audit``).

Uitloop (thermal_tail_seconds)
------------------------------
Warmte-uitwisseling begint en eindigt niet op het schakelmoment: bij het starten geven de
sensoren eerst stilstaand water door (sprongen in ΔT), na het uitschakelen koelt het toestel
nog enkele minuten af terwijl de pomp doorloopt. Alleen tellen tijdens de bedrijfstoestand
laat dat laatste weg. Daarom wordt na verwarmen/koelen het warmtevermogen nog maximaal
``thermal_tail_seconds`` meegeteld zolang de pomp draait en de ΔT positief blijft.

Persistentie
------------
Alle tellers staan in ``cop_calculator_data.json`` (in ``data_dir``) en worden:
  * atomair weggeschreven (tmp-bestand + fsync + os.replace),
  * elke ``save_interval`` seconden bewaard (en direct bij een DHW-run-einde,
    een dag/maand/jaar-wissel en bij het afsluiten van de app),
  * voorzien van een ``.prev``-kopie van de vorige goede versie,
  * geback-upt in ``backups/`` (dagbestand: ``backup_keep_days`` dagen, maandbestand:
    voor altijd; elk uur ververst), optioneel ook naar ``extra_backup_dir``.
Een corrupt bestand wordt nooit overschreven: het wordt hernoemd naar
``*.corrupt-<tijd>`` en de data wordt hersteld uit .prev / een back-up.
Afgesloten dagen, maanden en jaren worden bovendien gearchiveerd onder
``history`` in hetzelfde bestand, zodat ze na een reset niet verloren gaan.
"""
import datetime
import glob
import json
import math
import os
import shutil
import statistics
import time
import traceback

import hassapi as hass

SPECIFIC_HEAT_WATER = 4180.0  # J/(kg*K)
WATER_DENSITY = 1000.0  # kg/m3 (debiet in m3/h -> kg/s)
JOULES_PER_KWH = 3600000.0


class COPCalculator(hass.Hass):
    MODES = ("heating", "cooling", "dhw")
    SPACE_MODES = ("heating", "cooling")
    PERIODS = ("daily", "monthly", "yearly", "lifetime")
    CALENDAR_KEYS = {"daily": "last_day", "monthly": "last_month", "yearly": "last_year"}
    MODE_LABELS = {"heating": "Heating", "cooling": "Cooling", "dhw": "DHW"}
    PERIOD_LABELS = {
        "realtime": "Realtime",
        "daily": "Daily",
        "monthly": "Monthly",
        "yearly": "Yearly",
        "lifetime": "Lifetime",
    }

    DEFAULT_ENTITIES = {
        "indoor_power_entity": "sensor.shelly_warmtepomp_binnenunit_active_power",
        "outdoor_power_entity": "sensor.shelly_warmtepomp_buitenunit_active_power",
        "dhw_heater_entity": "binary_sensor.control_unit_dhw_heater_2",
        "own_outlet_temp_entity": "sensor.shelly_temperatuursensoren_warmtepomp_temperatuur",
        "own_inlet_temp_entity": "sensor.shelly_temperatuursensoren_warmtepomp_temperatuur_2",
        "builtin_outlet_temp_entity": "sensor.control_unit_water_outlet_temperature_2",
        "builtin_inlet_temp_entity": "sensor.control_unit_water_inlet_temperature_2",
        "flow_entity": "sensor.control_unit_water_flow_2",
        "operation_state_entity": "sensor.control_unit_operation_state_2",
        "dhw_current_temp_entity": "sensor.dhw_current_temperature",
        "indoor_energy_entity": "sensor.shellypro3em_441d64749708_phase_b_total_active_energy",
        "outdoor_energy_entity": "sensor.shellypro3em_441d64749708_phase_c_total_active_energy",
    }
    METERS = ("indoor", "outdoor")
    OPERATION_STATES = {
        "operation_state_heat_thermo_on": "heating",
        "operation_state_cool_thermo_on": "cooling",
        "operation_state_dhw_on": "dhw",
    }
    INVALID_STATES = ("", "unknown", "unavailable", "none")

    STORAGE_NAME = "cop_calculator_data.json"
    STATUS_ENTITY = "sensor.hitachi_yutaki_cop_calculator_status"
    REPUBLISH_SECONDS = 120  # ook ongewijzigde sensoren opnieuw zetten (HA-herstart)
    MIN_STALE_AFTER_SECONDS = 120
    UNKNOWN_FREQUENCY_STALE_AFTER_SECONDS = 300
    STALE_INTERVAL_MULTIPLIER = 3
    MAX_FREQUENCY_SAMPLES = 20
    COUNTER_GUARD_MIN_KWH = 0.03      # vanaf deze cumulatief bemonsterde energie bewaken we teller vs. meting
    COUNTER_GUARD_RANGE = (0.5, 2.0)  # toegestane (cumulatieve) verhouding teller / bemonsterd
    COUNTER_STATS_WINDOW_KWH = 5.0    # na zoveel bemonsterde kWh halveren we de statistiek (voortschrijdend)
    DHW_FLOW_COVERAGE = 0.8           # min. dekking van de debietmeting voor een geldige run-schatting
    CAL_MIN_OFF_SECONDS = 600         # compressor zo lang uit voordat een nul-ΔT-meting telt
    CAL_MIN_FLOW = 0.3                # m3/h, minimaal debiet voor een nul-ΔT-meting
    CAL_MAX_ABS_DELTA = 2.0           # K, grotere afwijkingen zijn geen offset maar echte warmteoverdracht
    CAL_MIN_SAMPLES = 20
    AUDIT_WINDOW_SECONDS = 3 * 86400  # voortschrijdend venster (bedrijfstijd) voor de ΔT-controle
    AUDIT_MIN_SECONDS = 1800
    CAL_MAX_SAMPLES = 1500            # totaal
    CAL_MAX_PER_BIN = 120             # per inlaattemperatuur-bin; oudste vallen eruit, zodat alle temperaturen bewaard blijven
    CAL_BIN_WIDTH = 5.0               # K, breedte van de inlaattemperatuur-bins
    CAL_MIN_BIN_SAMPLES = 10

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def initialize(self):
        self.entities = dict(self.DEFAULT_ENTITIES)
        custom_entities = self.args.get("entities")
        if isinstance(custom_entities, dict):
            self.entities.update(
                {key: value for key, value in custom_entities.items() if key in self.DEFAULT_ENTITIES and value}
            )

        legacy_keys = {
            "outlet_temp_entity": "own_outlet_temp_entity",
            "inlet_temp_entity": "own_inlet_temp_entity",
            "reference_outlet_temp_entity": "builtin_outlet_temp_entity",
            "reference_inlet_temp_entity": "builtin_inlet_temp_entity",
        }
        if isinstance(custom_entities, dict):
            for old_key, new_key in legacy_keys.items():
                if custom_entities.get(old_key):
                    self.warning(f"entities.{old_key} is vervangen door entities.{new_key}; waarde overgenomen.")
                    self.entities[new_key] = custom_entities[old_key]

        self.update_interval = max(5, self._arg("update_interval", 30, int))
        self.save_interval = max(self.update_interval, self._arg("save_interval", 300, int))
        self.max_gap_seconds = 3 * self.update_interval
        self.pump_power = self._arg("pump_power", 59, float)
        self.pump_power_max = self._arg("pump_power_max", 250, float)
        self.dhw_tank_volume = self._arg("dhw_tank_volume", 260, float)
        self.dhw_temp_deadband = self._arg("dhw_temp_deadband", 0.3, float)
        self.dhw_end_grace = self._arg("dhw_end_grace", 180, float)
        self.dhw_max_run_seconds = self._arg("dhw_max_run_hours", 12, float) * 3600
        self.min_run_electrical_kwh = self._arg("min_run_electrical_kwh", 0.02, float)
        self.max_stored_dhw_runs = max(1, self._arg("max_stored_dhw_runs", 10, int))
        self.allow_negative_thermal = self._arg("allow_negative_thermal", True, self._to_bool)
        self.use_energy_counters = self._arg("use_energy_counters", True, self._to_bool)
        self.energy_counter_timeout = self._arg("energy_counter_timeout", 900, float)
        self.delta_t_offset = self._arg("delta_t_offset", 0.0, float)  # voor de eigen sensoren
        self.builtin_delta_t_offset = self._arg("builtin_delta_t_offset", 0.0, float)
        self.temperature_source = str(self.args.get("temperature_source", "auto")).strip().lower()
        if self.temperature_source not in ("own", "builtin", "auto"):
            self.warning(f"Onbekende temperature_source '{self.temperature_source}', 'auto' gebruikt.")
            self.temperature_source = "auto"
        self.dhw_temperature_source = str(self.args.get("dhw_temperature_source", "builtin")).strip().lower()
        if self.dhw_temperature_source not in ("own", "builtin", "auto"):
            self.warning(f"Onbekende dhw_temperature_source '{self.dhw_temperature_source}', 'builtin' gebruikt.")
            self.dhw_temperature_source = "builtin"
        self.own_max_deviation = max(0.0, self._arg("own_max_deviation", 5.0, float))
        self.source_switch_seconds = max(0.0, self._arg("source_switch_seconds", 120, float))
        self.flow_correction_factor = self._arg("flow_correction_factor", 1.0, float)
        self.delta_t_offset_curve = self._parse_curve(self.args.get("delta_t_offset_curve"))
        self.thermal_tail_seconds = max(0.0, self._arg("thermal_tail_seconds", 300, float))
        self.dhw_thermal_method = str(self.args.get("dhw_thermal_method", "tank")).strip().lower()
        if self.dhw_thermal_method not in ("tank", "flow"):
            self.warning(f"Onbekende dhw_thermal_method '{self.dhw_thermal_method}', 'tank' gebruikt.")
            self.dhw_thermal_method = "tank"
        self.calibration_max_outdoor_power = self._arg("calibration_max_outdoor_power", 30, float)
        self.backup_keep_days = max(1, self._arg("backup_keep_days", 30, int))
        self.backup_interval = max(self.save_interval, self._arg("backup_interval", 3600, int))
        self.history_keep_days = max(31, self._arg("history_keep_days", 400, int))

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = self.args.get("data_dir") or os.path.join(base_dir, "cop_calculator_data")
        self.storage_file = os.path.join(self.data_dir, self.STORAGE_NAME)
        self.backup_dir = os.path.join(self.data_dir, "backups")
        self.extra_backup_dir = self.args.get("extra_backup_dir") or None
        self.legacy_file = os.path.join(base_dir, self.STORAGE_NAME)
        self.import_file = self.args.get("import_file") or None
        if self.import_file and not os.path.isabs(self.import_file):
            self.import_file = os.path.join(self.data_dir, self.import_file)

        now = self._now()
        self.data = self._default_data(now)
        self.realtime = {mode: {"electrical": 0.0, "thermal": 0.0} for mode in self.SPACE_MODES}
        self._source_values = {}
        self._source_history = {}
        self._stale_sources = []
        self._status = "starting"
        self._published = {}
        self._source_units = {}
        self._prev_sample = None
        self._prev_dhw_thermal = None
        self._tail = None
        self._own_ok_since = None
        self._source_in_use = None
        self._temp_info = {"setting": self.temperature_source, "deviating": False}
        self._prev_power = {meter: None for meter in self.METERS}
        self._sampled = {meter: {} for meter in self.METERS}
        self._counters = {
            meter: {"last": None, "stuck": None, "changed_at": 0.0, "sampled_kwh": 0.0, "counter_kwh": 0.0}
            for meter in self.METERS
        }
        self._warned = {}
        self._compressor_off_seconds = 0.0
        self._dirty = False
        self._last_backup_monotonic = None
        self._last_backup_iso = None
        self._last_saved_iso = None

        loaded_from = self._load_data()
        self._init_calendar(now)
        self._check_storage_writable()

        self._force_save = loaded_from != "hoofdbestand"  # nieuw of hersteld -> direct wegschrijven
        self._last_save_monotonic = time.monotonic()
        self._last_update_monotonic = time.monotonic()
        self._publish_sensors(now)
        self.run_every(self._update, "now", self.update_interval)
        self.log(f"COP Calculator gestart. Data: {self.storage_file}")

    def terminate(self):
        if getattr(self, "data", None) is not None:
            self._save_data()

    # ------------------------------------------------------------------
    # Hulpfuncties
    # ------------------------------------------------------------------
    @staticmethod
    def _to_bool(value):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "ja")
        return bool(value)

    @staticmethod
    def _num(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    def _arg(self, name, default, cast=float):
        value = self.args.get(name, default)
        try:
            return cast(value)
        except (TypeError, ValueError):
            self.warning(f"Ongeldige waarde voor '{name}': {value!r}. Standaardwaarde {default!r} gebruikt.")
            return cast(default)

    def _now(self):
        """Tijdzone-bewuste 'nu' in de AppDaemon-tijdzone."""
        try:
            now = self.get_now()
        except Exception:  # noqa: BLE001
            now = None
        if isinstance(now, datetime.datetime) and now.tzinfo is not None:
            return now
        return datetime.datetime.now().astimezone()

    @staticmethod
    def _localize(naive, tzinfo):
        if hasattr(tzinfo, "localize"):  # pytz
            return tzinfo.localize(naive)
        return naive.replace(tzinfo=tzinfo)

    def _period_start_iso(self, period, now):
        if period == "daily":
            naive = datetime.datetime(now.year, now.month, now.day)
        elif period == "monthly":
            naive = datetime.datetime(now.year, now.month, 1)
        else:
            naive = datetime.datetime(now.year, 1, 1)
        return self._localize(naive, now.tzinfo).isoformat()

    @staticmethod
    def _cop(thermal, electrical):
        if electrical is None or electrical < 0.001:
            return "unknown"
        return round(max(0.0, thermal) / electrical, 2)

    # ------------------------------------------------------------------
    # Datamodel
    # ------------------------------------------------------------------
    def _default_data(self, now):
        data = {
            "schema_version": 2,
            "meta": {"created": now.isoformat(), "lifetime_start": now.isoformat(), "last_saved": None, "cal_samples": [], "tail_kwh": {"heating": 0.0, "cooling": 0.0}, "delta_t_audit": {}, "temp_source_seconds": {"own": 0.0, "builtin": 0.0}},
            "dhw": {"runs": [], "current_run": None},
            "calendar": {"last_day": None, "last_month": None, "last_year": None},
            "history": {"daily": {}, "monthly": {}, "yearly": {}},
        }
        for period in self.PERIODS:
            data[period] = {mode: {"energy": {"electrical": 0.0, "thermal": 0.0}} for mode in self.MODES}
        return data

    def _merge_data(self, target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._merge_data(target[key], value)
            else:
                target[key] = value

    def _normalize_run(self, run):
        if not isinstance(run, dict):
            return None
        try:
            start_temp = float(run["start_temp"])
            last_temp = float(run.get("last_temp", start_temp))
        except (KeyError, TypeError, ValueError):
            return None
        run = dict(run)
        run["start_temp"] = start_temp
        run["last_temp"] = last_temp
        run["ref_temp"] = self._num(run["ref_temp"]) if "ref_temp" in run else last_temp
        if "rise_sum" not in run:  # run uit oudere versie
            run["rise_sum"] = max(0.0, last_temp - start_temp) + self._num(run.get("sum_of_drops"))
        run.pop("sum_of_drops", None)
        for key in (
            "rise_sum", "electrical", "thermal", "heater_kwh", "loop_kwh", "flow_seconds",
            "compressor_seconds", "inactive_seconds", "total_seconds",
        ):
            run[key] = self._num(run.get(key))
        run.setdefault("start_time", None)
        return run

    def _normalize_data(self):
        """Herstel structuur en types na het laden (ook van oudere bestanden/integratie-opslag)."""
        data = self.data
        for legacy_key in ("heating", "cooling"):  # alleen nog in oude bestanden
            data.pop(legacy_key, None)

        for period in self.PERIODS:
            section = data.get(period) if isinstance(data.get(period), dict) else {}
            fixed = {}
            for mode in self.MODES:
                entry = section.get(mode) if isinstance(section.get(mode), dict) else {}
                energy = entry.get("energy") if isinstance(entry.get("energy"), dict) else {}
                fixed[mode] = {
                    "energy": {
                        "electrical": self._num(energy.get("electrical")),
                        "thermal": self._num(energy.get("thermal")),
                    }
                }
            data[period] = fixed

        dhw = data.get("dhw") if isinstance(data.get("dhw"), dict) else {}
        runs = dhw.get("runs") if isinstance(dhw.get("runs"), list) else []
        runs = [run for run in runs if isinstance(run, dict)]
        data["dhw"] = {
            "runs": runs[-self.max_stored_dhw_runs:],
            "current_run": self._normalize_run(dhw.get("current_run")),
        }

        calendar = data.get("calendar") if isinstance(data.get("calendar"), dict) else {}
        data["calendar"] = {
            key: (str(calendar.get(key)) if calendar.get(key) else None)
            for key in ("last_day", "last_month", "last_year")
        }

        history = data.get("history") if isinstance(data.get("history"), dict) else {}
        data["history"] = {
            period: (history.get(period) if isinstance(history.get(period), dict) else {})
            for period in ("daily", "monthly", "yearly")
        }

        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        meta.setdefault("created", None)
        meta.setdefault("lifetime_start", None)
        meta.setdefault("last_saved", None)
        cleaned = []
        for pair in meta.get("cal_samples") if isinstance(meta.get("cal_samples"), list) else []:
            try:
                cleaned.append([float(pair[0]), float(pair[1])])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        meta["cal_samples"] = cleaned[-self.CAL_MAX_SAMPLES:]
        tail = meta.get("tail_kwh") if isinstance(meta.get("tail_kwh"), dict) else {}
        meta["tail_kwh"] = {mode: self._num(tail.get(mode)) for mode in self.SPACE_MODES}
        seconds = meta.get("temp_source_seconds") if isinstance(meta.get("temp_source_seconds"), dict) else {}
        meta["temp_source_seconds"] = {key: self._num(seconds.get(key)) for key in ("own", "builtin")}
        audit = meta.get("delta_t_audit") if isinstance(meta.get("delta_t_audit"), dict) else {}
        meta["delta_t_audit"] = {
            mode: {key: self._num(audit[mode].get(key)) for key in ("seconds", "own", "ref")}
            for mode in self.MODES
            if isinstance(audit.get(mode), dict)
        }
        data["meta"] = meta
        data["schema_version"] = 2

    # ------------------------------------------------------------------
    # Persistentie: laden
    # ------------------------------------------------------------------
    def _read_json(self, path):
        """Lees een JSON-bestand; geef dict terug of None als het onleesbaar/ongeldig is."""
        try:
            with open(path, "r", encoding="utf-8") as data_file:
                content = json.load(data_file)
        except (OSError, ValueError) as error:
            self.warning(f"Kon {path} niet lezen: {error}")
            return None
        if isinstance(content, dict) and isinstance(content.get("data"), dict) and "key" in content:
            content = content["data"]  # HA .storage-bestand van de oude integratie
        if not isinstance(content, dict):
            return None
        if not any(key in content for key in ("lifetime", "monthly", "yearly", "daily", "calendar")):
            self.warning(f"{path} lijkt geen COP-data te bevatten.")
            return None
        return content

    def _candidate_files(self):
        candidates = [("hoofdbestand", self.storage_file), ("vorige versie", f"{self.storage_file}.prev")]
        directories = [d for d in (self.backup_dir, self.extra_backup_dir) if d]
        for directory in directories:
            pattern = os.path.join(directory, "cop_calculator_20??-??-??.json")
            for path in sorted(glob.glob(pattern), reverse=True):
                candidates.append(("dagelijkse back-up", path))
        for directory in directories:
            pattern = os.path.join(directory, "cop_calculator_monthly_*.json")
            for path in sorted(glob.glob(pattern), reverse=True):
                candidates.append(("maandelijkse back-up", path))
        if self.import_file:
            candidates.append(("import", self.import_file))
        candidates.append(("oud bestand naast het script", self.legacy_file))
        return candidates

    def _quarantine(self, path):
        target = f"{path}.corrupt-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, target)
            self.error(f"Corrupt bestand bewaard als {target}")
        except OSError as error:
            self.error(f"Kon corrupt bestand {path} niet hernoemen: {error}")

    def _load_data(self):
        """Laad de data. Geeft het label van de bron terug, of None bij een schone start."""
        problems = []
        for label, path in self._candidate_files():
            if not os.path.isfile(path):
                continue
            content = self._read_json(path)
            if content is None:
                problems.append(path)
                if label in ("hoofdbestand", "vorige versie"):
                    self._quarantine(path)
                continue
            self._merge_data(self.data, content)
            self._normalize_data()
            self.log(f"COP-data geladen uit {label}: {path}")
            if label != "hoofdbestand" or problems:
                self._notify(
                    f"COP-data is hersteld uit '{label}' ({path}). "
                    "Controleer de lifetime-, jaar- en maandwaarden."
                )
            return label
        if problems:
            self._notify(
                "COP-data kon NIET worden geladen: alle bestanden waren onleesbaar "
                f"({', '.join(problems)}). De corrupte bestanden zijn bewaard; "
                "er is met lege tellers gestart."
            )
        return None

    def _check_storage_writable(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            probe = os.path.join(self.data_dir, ".write_test")
            with open(probe, "w", encoding="utf-8") as probe_file:
                probe_file.write("ok")
            os.remove(probe)
        except OSError as error:
            self._notify(f"COP Calculator kan niet schrijven naar {self.data_dir}: {error}")

    def _notify(self, message):
        self.error(message)
        try:
            self.call_service(
                "persistent_notification/create",
                title="COP Calculator",
                message=message,
                notification_id="cop_calculator_storage",
            )
        except Exception as error:  # noqa: BLE001
            self.warning(f"Melding in Home Assistant mislukt: {error}")

    # ------------------------------------------------------------------
    # Persistentie: opslaan en back-ups
    # ------------------------------------------------------------------
    def _save_data(self, now=None):
        now = now or self._now()
        self.data["meta"]["last_saved"] = now.isoformat()
        temporary_file = f"{self.storage_file}.tmp"
        try:
            payload = json.dumps(self.data, ensure_ascii=True, separators=(",", ":"))
            os.makedirs(self.data_dir, exist_ok=True)
            with open(temporary_file, "w", encoding="utf-8") as data_file:
                data_file.write(payload)
                data_file.flush()
                os.fsync(data_file.fileno())
            if os.path.isfile(self.storage_file) and self._read_json(self.storage_file) is not None:
                shutil.copy2(self.storage_file, f"{self.storage_file}.prev")
            os.replace(temporary_file, self.storage_file)
        except (OSError, TypeError, ValueError) as error:
            self.error(f"COP-data kon niet worden opgeslagen: {error}")
            try:
                os.remove(temporary_file)
            except OSError:
                pass
            return False
        self._dirty = False
        self._last_saved_iso = now.isoformat()
        return True

    def _maybe_save(self, now, monotonic_now):
        due = self._dirty and monotonic_now - self._last_save_monotonic >= self.save_interval
        if not (self._force_save or due):
            return
        self._force_save = False
        self._last_save_monotonic = monotonic_now
        if self._save_data(now):
            self._maybe_backup(now, monotonic_now)

    def _lifetime_total(self, data):
        """Totaal elektrisch verbruik (kWh, alle modi) in een dataset; 0 bij een ongeldige dataset."""
        try:
            return sum(float(data["lifetime"][mode]["energy"]["electrical"]) for mode in self.MODES)
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _copy_atomic(self, source, target):
        temporary = f"{target}.tmp"
        shutil.copy2(source, temporary)
        os.replace(temporary, target)

    def _maybe_backup(self, now, monotonic_now):
        """Ververs de dag- en maandback-up (max. 1x per backup_interval).

        Het dagbestand eindigt zo met de eindstand van die dag, het maandbestand met de
        eindstand van die maand (blijft altijd bewaard). Een back-up wordt nooit
        overschreven door een dataset met minder lifetime-verbruik (bv. na een foute reset).
        """
        current_total = self._lifetime_total(self.data)
        if current_total <= 0:
            return
        if (
            self._last_backup_monotonic is not None
            and monotonic_now - self._last_backup_monotonic < self.backup_interval
        ):
            return
        self._last_backup_monotonic = monotonic_now
        names = (
            f"cop_calculator_{now.date().isoformat()}.json",
            f"cop_calculator_monthly_{now.year}-{now.month:02d}.json",
        )
        for directory in (self.backup_dir, self.extra_backup_dir):
            if not directory:
                continue
            try:
                os.makedirs(directory, exist_ok=True)
                for name in names:
                    path = os.path.join(directory, name)
                    if os.path.exists(path):
                        existing = self._read_json(path)
                        if existing is not None and self._lifetime_total(existing) > current_total + 1e-9:
                            self.warning(
                                f"Back-up {path} niet overschreven: bevat meer verbruik dan de huidige data."
                            )
                            continue
                    self._copy_atomic(self.storage_file, path)
                self._prune_backups(directory, now)
            except OSError as error:
                self.warning(f"Back-up naar {directory} mislukt: {error}")
        self._last_backup_iso = now.isoformat()

    def _prune_backups(self, directory, now):
        cutoff = now.date() - datetime.timedelta(days=self.backup_keep_days)
        prefix, suffix = "cop_calculator_", ".json"
        for path in glob.glob(os.path.join(directory, "cop_calculator_20??-??-??.json")):
            stamp = os.path.basename(path)[len(prefix):-len(suffix)]
            try:
                day = datetime.date.fromisoformat(stamp)
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Kalender, resets en archief
    # ------------------------------------------------------------------
    def _current_labels(self, now):
        return {
            "daily": now.date().isoformat(),
            "monthly": f"{now.year}-{now.month:02d}",
            "yearly": str(now.year),
        }

    def _init_calendar(self, now):
        calendar = self.data["calendar"]
        for period, label in self._current_labels(now).items():
            key = self.CALENDAR_KEYS[period]
            if calendar.get(key) is None:
                calendar[key] = label
        if not self.data["meta"].get("lifetime_start"):
            self.data["meta"]["lifetime_start"] = now.isoformat()

    def _archive_period(self, period, label):
        snapshot = {}
        for mode in self.MODES:
            energy = self.data[period][mode]["energy"]
            snapshot[mode] = {
                "electrical": round(energy["electrical"], 4),
                "thermal": round(energy["thermal"], 4),
            }
        if not any(values["electrical"] > 0 or values["thermal"] != 0 for values in snapshot.values()):
            return
        archive = self.data["history"][period]
        archive[label] = snapshot
        if period == "daily" and len(archive) > self.history_keep_days:
            for old_label in sorted(archive)[: len(archive) - self.history_keep_days]:
                del archive[old_label]

    def _update_calendar(self, now):
        calendar = self.data["calendar"]
        resets = []
        for period, current in self._current_labels(now).items():
            key = self.CALENDAR_KEYS[period]
            previous = calendar.get(key)
            if previous != current:
                if previous is not None:
                    self._archive_period(period, previous)
                    for mode in self.MODES:
                        self.data[period][mode]["energy"] = {"electrical": 0.0, "thermal": 0.0}
                    resets.append(period)
                calendar[key] = current
        if resets:
            self.log(f"COP-tellers gereset en gearchiveerd voor: {', '.join(resets)}.")
            self._dirty = True
            self._force_save = True

    def _add_energy(self, mode, electrical_kwh, thermal_kwh):
        if not (math.isfinite(electrical_kwh) and math.isfinite(thermal_kwh)):
            return
        for period in self.PERIODS:
            energy = self.data[period][mode]["energy"]
            energy["electrical"] += electrical_kwh
            energy["thermal"] += thermal_kwh
        self._dirty = True

    # ------------------------------------------------------------------
    # Bronnen lezen
    # ------------------------------------------------------------------
    def _get_state_snapshot(self, entity_id):
        try:
            state = self.get_state(entity_id, attribute="all")
        except TypeError:
            state = self.get_state(entity_id)
        if isinstance(state, dict) and "state" in state:
            attributes = state.get("attributes")
            unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
            return state.get("state"), state.get("last_reported") or state.get("last_updated"), unit
        return state, None, None

    @staticmethod
    def _parse_update_time(value):
        if isinstance(value, datetime.datetime):
            updated_at = value
        elif isinstance(value, str):
            try:
                updated_at = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
        return updated_at.astimezone(datetime.timezone.utc)

    def _capture_source_snapshot(self, now):
        """Lees alle bronnen één keer per tick en bepaal welke ongewoon lang niet zijn ververst.

        Let op: AppDaemon cachet alleen state_changed-events. Een sensor die lang dezelfde
        waarde houdt, lijkt dus 'stale'. Daarom is dit uitsluitend diagnostiek (statussensor)
        en wordt er nooit op gefilterd.
        """
        now_utc = now.astimezone(datetime.timezone.utc)
        values = {}
        stale = []
        for entity_id in self.entities.values():
            state, stamp, unit = self._get_state_snapshot(entity_id)
            values[entity_id] = state
            self._source_units[entity_id] = unit
            updated_at = self._parse_update_time(stamp)
            history = self._source_history.setdefault(entity_id, {"last": None, "intervals": []})
            age = None
            if updated_at is not None:
                previous = history["last"]
                if previous is not None and updated_at > previous:
                    history["intervals"].append((updated_at - previous).total_seconds())
                    del history["intervals"][: -self.MAX_FREQUENCY_SAMPLES]
                history["last"] = updated_at
                age = max(0.0, (now_utc - updated_at).total_seconds())
            if history["intervals"]:
                typical = statistics.median(history["intervals"])
                stale_after = max(self.MIN_STALE_AFTER_SECONDS, typical * self.STALE_INTERVAL_MULTIPLIER)
            else:
                stale_after = self.UNKNOWN_FREQUENCY_STALE_AFTER_SECONDS
            if age is not None and age > stale_after:
                stale.append(entity_id)
        self._source_values = values
        self._stale_sources = sorted(stale)

    def _get_float(self, key):
        value = self._source_values.get(self.entities[key])
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    # ------------------------------------------------------------------
    # Elektrische energie: Shelly-tellers + gemeten vermogens
    # ------------------------------------------------------------------
    def _get_energy_kwh(self, meter):
        entity_id = self.entities[f"{meter}_energy_entity"]
        try:
            number = float(self._source_values.get(entity_id))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        unit = str(self._source_units.get(entity_id) or "kWh").strip().lower()
        return number * {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}.get(unit, 1.0)

    def _indoor_categories(self, indoor_power, heater_on, category):
        """Verdeel de binnenunit-meting over {categorie: W}; het DHW-element is 'element'.

        Staat het element uit, dan is de meting de pomp (volgt het toerental). Staat het aan,
        dan wordt de vaste pompschatting afgetrokken en de rest is het element.
        """
        if heater_on:
            if indoor_power is None:
                return None
            element = max(0.0, indoor_power - self.pump_power)
            return {category: indoor_power - element, "element": element}
        if indoor_power is not None and 0.0 <= indoor_power <= self.pump_power_max:
            return {category: indoor_power}
        return {category: self.pump_power if category != "idle" else 0.0}

    def _sample_electrical(self, elapsed, current_by_meter):
        """Trapeziumregel per categorie; de uitkomst gaat in de buffer van de betreffende meter."""
        hours = elapsed / 3600.0
        for meter, current in current_by_meter.items():
            previous = self._prev_power.get(meter) or {}
            current_powers = current or {}
            buffer = self._sampled[meter]
            for category in set(previous) | set(current_powers):
                kwh = (previous.get(category, 0.0) + current_powers.get(category, 0.0)) / 2.0 * hours / 1000.0
                if kwh:
                    buffer[category] = buffer.get(category, 0.0) + kwh
            self._prev_power[meter] = current

    def _warn_throttled(self, key, message):
        now = time.monotonic()
        if now - self._warned.get(key, -1e9) > 6 * 3600:
            self._warned[key] = now
            self.warning(message)

    def _apply_electrical(self, energies):
        for category, kwh in energies.items():
            if not math.isfinite(kwh) or kwh <= 0:
                continue
            if category in self.SPACE_MODES:
                self._add_energy(category, kwh, 0.0)
            elif category in ("dhw", "element"):
                run = self.data["dhw"]["current_run"]
                if run is not None:
                    run["electrical"] += kwh
                    if category == "element":
                        run["heater_kwh"] += kwh
                    self._dirty = True
            # 'idle' (stand-by e.d.) telt niet mee in de COP

    def _flush(self, buffer, scale):
        if buffer:
            energies = {category: kwh * scale for category, kwh in buffer.items()}
            buffer.clear()
            self._apply_electrical(energies)

    def _flush_all(self):
        """Neem de bemonsterde waarden ongeschaald over (bij een gap of onbekende toestand)."""
        for meter in self.METERS:
            self._flush(self._sampled[meter], 1.0)

    def _force_flush(self, categories):
        for meter in self.METERS:
            buffer = self._sampled[meter]
            pending = {category: buffer.pop(category) for category in categories if category in buffer}
            if pending:
                self._apply_electrical(pending)

    def _reset_baselines(self):
        """Na een gap of onbekende toestand: de eerstvolgende tellerwijziging bevat energie die we
        niet hebben bemonsterd. Die wijziging dient daarom alleen als nieuwe basislijn."""
        for meter in self.METERS:
            track = self._counters[meter]
            track["stuck"] = self._get_energy_kwh(meter) if track["last"] is None else track["last"]
            track["last"] = None

    def _settle_meter(self, meter, counter_kwh, monotonic_now):
        """Schaal de bemonsterde energie naar de teller zodra die verandert."""
        buffer = self._sampled[meter]
        track = self._counters[meter]
        if not self.use_energy_counters:
            self._flush(buffer, 1.0)
            return
        if track["last"] is None:
            # Geen basislijn: meetwaarden ongeschaald tellen tot de teller een nieuwe waarde heeft.
            self._flush(buffer, 1.0)
            if counter_kwh is not None and counter_kwh != track["stuck"]:
                track["last"], track["changed_at"], track["stuck"] = counter_kwh, monotonic_now, None
            return
        if counter_kwh is not None and counter_kwh < track["last"] - 1e-6:
            self._warn_throttled(
                f"reset-{meter}", f"Energieteller {meter} liep terug (reset/vervanging?). Nieuwe basislijn."
            )
            self._flush(buffer, 1.0)
            track["last"], track["changed_at"] = counter_kwh, monotonic_now
            return
        if counter_kwh is not None and counter_kwh > track["last"] + 1e-9:
            delta = counter_kwh - track["last"]
            track["last"], track["changed_at"] = counter_kwh, monotonic_now
            total = sum(buffer.values())
            if total <= 1e-9:  # niets bemonsterd (sensor offline): energie niet toewijsbaar
                buffer.clear()
                return
            track["sampled_kwh"] += total
            track["counter_kwh"] += delta
            scale = delta / total
            if track["sampled_kwh"] >= self.COUNTER_GUARD_MIN_KWH:
                ratio = track["counter_kwh"] / track["sampled_kwh"]
                low, high = self.COUNTER_GUARD_RANGE
                if not (low <= ratio <= high):
                    self._warn_throttled(
                        f"scale-{meter}",
                        f"Energieteller {meter} wijkt sterk af van het gemeten vermogen (factor {ratio:.2f}). "
                        "Controleer entiteit/eenheid; gemeten waarden worden ongeschaald gebruikt.",
                    )
                    scale = 1.0
            if track["sampled_kwh"] > self.COUNTER_STATS_WINDOW_KWH:
                track["sampled_kwh"] *= 0.5
                track["counter_kwh"] *= 0.5
            self._flush(buffer, scale)
            return
        # Teller ongewijzigd of onbeschikbaar.
        if sum(buffer.values()) > 0.01 and monotonic_now - track["changed_at"] > self.energy_counter_timeout:
            self._warn_throttled(
                f"stale-{meter}",
                f"Energieteller {meter} is al {self.energy_counter_timeout:.0f}s niet veranderd; "
                "terugval op gemeten vermogen.",
            )
            self._flush(buffer, 1.0)
            track["stuck"], track["last"] = track["last"], None

    def _settle_all(self, monotonic_now):
        for meter in self.METERS:
            self._settle_meter(meter, self._get_energy_kwh(meter), monotonic_now)

    # ------------------------------------------------------------------
    # Thermisch vermogen van het primaire circuit
    # ------------------------------------------------------------------
    def _parse_curve(self, raw):
        if not raw:
            return []
        try:
            return sorted((float(temp), float(offset)) for temp, offset in raw)
        except (TypeError, ValueError):
            self.warning("Ongeldige delta_t_offset_curve (verwacht [[temperatuur, offset], ...]); genegeerd.")
            return []

    def _delta_t_offset_at(self, inlet_temp):
        """Offset (K) voor de gegeven inlaattemperatuur: lineair geïnterpoleerde curve of vaste waarde."""
        curve = self.delta_t_offset_curve
        if not curve or inlet_temp is None:
            return self.delta_t_offset
        if inlet_temp <= curve[0][0]:
            return curve[0][1]
        if inlet_temp >= curve[-1][0]:
            return curve[-1][1]
        for (temp0, offset0), (temp1, offset1) in zip(curve, curve[1:]):
            if temp0 <= inlet_temp <= temp1:
                if temp1 == temp0:
                    return offset1
                return offset0 + (offset1 - offset0) * (inlet_temp - temp0) / (temp1 - temp0)
        return self.delta_t_offset

    def _offset_for(self, source, inlet_temp):
        if source == "builtin":
            return self.builtin_delta_t_offset
        return self._delta_t_offset_at(inlet_temp)

    @staticmethod
    def _plausible_temp(value):
        return value is not None and -30.0 < value < 120.0

    def _read_pair(self, outlet_key, inlet_key):
        outlet = self._get_float(outlet_key)
        inlet = self._get_float(inlet_key)
        if self._plausible_temp(outlet) and self._plausible_temp(inlet):
            return (outlet, inlet)
        return None

    def _temperature_setting(self):
        """Gewenste bron: config, eventueel live overschreven door een input_select."""
        setting = self.temperature_source
        entity_id = self.args.get("temperature_source_entity")
        if entity_id:
            try:
                value = self.get_state(entity_id)
            except Exception:  # noqa: BLE001
                value = None
            if isinstance(value, str) and value.strip().lower() in ("own", "builtin", "auto"):
                setting = value.strip().lower()
        return setting

    def _select_temperatures(self, monotonic_now):
        """Kies één bijpassend paar (uitlaat, inlaat); nooit mengen tussen de sensorsets."""
        own = self._read_pair("own_outlet_temp_entity", "own_inlet_temp_entity")
        builtin = self._read_pair("builtin_outlet_temp_entity", "builtin_inlet_temp_entity")
        setting = self._temperature_setting()

        deviating = False
        if own and builtin and self.own_max_deviation > 0:
            deviating = max(abs(own[0] - builtin[0]), abs(own[1] - builtin[1])) > self.own_max_deviation
            if deviating:
                self._warn_throttled(
                    "own-deviation",
                    f"Eigen temperatuursensoren wijken > {self.own_max_deviation:.1f} K af van de ingebouwde "
                    "(sensor losgeraakt?). In 'auto' wordt op de ingebouwde teruggevallen.",
                )

        own_usable = own is not None and not deviating
        if own_usable:
            if self._own_ok_since is None:
                # bij de start (of na totale uitval) direct beschikbaar, anders even afwachten
                self._own_ok_since = monotonic_now - (self.source_switch_seconds if self._source_in_use is None else 0.0)
        else:
            self._own_ok_since = None
        own_ready = own_usable and monotonic_now - self._own_ok_since >= self.source_switch_seconds

        if setting == "own":
            source, pair = ("own", own) if own else (None, None)
        elif setting == "builtin":
            source, pair = ("builtin", builtin) if builtin else (None, None)
        elif own_ready:
            source, pair = "own", own
        elif builtin:
            source, pair = "builtin", builtin
        elif own_usable:
            source, pair = "own", own
        else:
            source, pair = None, None

        if source != self._source_in_use and self._source_in_use is not None and source is not None:
            self._warn_throttled(f"switch-{source}", f"Temperatuurbron gewijzigd: {self._source_in_use} -> {source}.")
        self._source_in_use = source

        # Aparte keuze voor het DHW-circuit (zie docstring): standaard alleen ingebouwde sensoren.
        if self.dhw_temperature_source == "own":
            dhw_source, dhw_pair = ("own", own) if own else (None, None)
        elif self.dhw_temperature_source == "builtin":
            dhw_source, dhw_pair = ("builtin", builtin) if builtin else (None, None)
        else:
            dhw_source, dhw_pair = source, pair
        self._temp_info = {"setting": setting, "deviating": deviating}
        return {
            "source": source,
            "outlet": pair[0] if pair else None,
            "inlet": pair[1] if pair else None,
            "dhw_source": dhw_source,
            "dhw_outlet": dhw_pair[0] if dhw_pair else None,
            "dhw_inlet": dhw_pair[1] if dhw_pair else None,
            "own": own,
            "builtin": builtin,
        }

    def _loop_thermal_power(self, mode, outlet_temp, inlet_temp, flow, source="own"):
        """Warmtevermogen in W (negatief bij warmte-onttrekking); None als een bron ontbreekt."""
        if None in (outlet_temp, inlet_temp, flow):
            return None
        delta_temp = outlet_temp - inlet_temp - self._offset_for(source, inlet_temp)
        if mode == "cooling":
            delta_temp = -delta_temp
        power = flow * WATER_DENSITY / 3600.0 * SPECIFIC_HEAT_WATER * delta_temp * self.flow_correction_factor
        if power < 0 and not self.allow_negative_thermal:
            power = 0.0
        return power

    def _track_delta_t_calibration(self, elapsed, outdoor_power, dhw_heater, flow, own_pair):
        """Verzamel nul-ΔT-metingen: compressor al lang uit, pomp draait -> uitlaat = inlaat.

        Elke meting is (inlaattemperatuur, uitlaat - inlaat). De metingen worden bewaard in
        het databestand, zodat een suggestie een herstart overleeft.
        """
        if outdoor_power is None or outdoor_power >= self.calibration_max_outdoor_power:
            self._compressor_off_seconds = 0.0
            return
        self._compressor_off_seconds += elapsed
        if (
            self._compressor_off_seconds < self.CAL_MIN_OFF_SECONDS
            or dhw_heater
            or own_pair is None
            or flow is None
            or flow < self.CAL_MIN_FLOW
        ):
            return
        outlet_temp, inlet_temp = own_pair  # alleen de eigen (decimale) sensoren worden gekalibreerd
        raw_delta = outlet_temp - inlet_temp
        if abs(raw_delta) <= self.CAL_MAX_ABS_DELTA:
            samples = self.data["meta"]["cal_samples"]
            samples.append([round(inlet_temp, 1), round(raw_delta, 2)])
            new_bin = math.floor(inlet_temp / self.CAL_BIN_WIDTH)
            in_bin = [i for i, (temp, _) in enumerate(samples) if math.floor(temp / self.CAL_BIN_WIDTH) == new_bin]
            if len(in_bin) > self.CAL_MAX_PER_BIN:
                del samples[in_bin[0]]
            del samples[: -self.CAL_MAX_SAMPLES]
            self._dirty = True

    def _delta_t_calibration(self):
        samples = self.data["meta"]["cal_samples"]
        result = {"offset": None, "samples": len(samples), "spread": None, "by_temp": {}, "curve": []}
        if len(samples) < self.CAL_MIN_SAMPLES:
            return result
        raw_values = [raw for _, raw in samples]
        result["offset"] = round(statistics.median(raw_values), 2)
        result["spread"] = round(statistics.pstdev(raw_values), 2)
        bins = {}
        for inlet, raw in samples:
            low = int(math.floor(inlet / self.CAL_BIN_WIDTH) * self.CAL_BIN_WIDTH)
            bins.setdefault(low, []).append(raw)
        for low in sorted(bins):
            values = bins[low]
            if len(values) >= self.CAL_MIN_BIN_SAMPLES:
                median = round(statistics.median(values), 2)
                label = f"{low}-{low + int(self.CAL_BIN_WIDTH)}"
                result["by_temp"][label] = {"offset": median, "spread": round(statistics.pstdev(values), 2), "n": len(values)}
                result["curve"].append([low + self.CAL_BIN_WIDTH / 2.0, median])
        return result

    # ------------------------------------------------------------------
    # Hoofdloop
    # ------------------------------------------------------------------
    def _reset_samples(self):
        self._tail = None
        self._prev_sample = None
        self._prev_dhw_thermal = None
        self._prev_power = {meter: None for meter in self.METERS}

    def _update(self, kwargs):
        try:
            now = self._now()
            monotonic_now = time.monotonic()
            raw_elapsed = monotonic_now - self._last_update_monotonic
            self._last_update_monotonic = monotonic_now
            if raw_elapsed > self.max_gap_seconds:
                self.warning(
                    f"Update-gap van {raw_elapsed:.0f}s (AppDaemon geblokkeerd?). "
                    "Deze periode wordt niet meegeteld."
                )
                self._flush_all()
                self._reset_baselines()
                self._reset_samples()
                elapsed = 0.0
            else:
                elapsed = max(0.0, raw_elapsed)
            self._update_data(now, elapsed, monotonic_now)
            self._publish_sensors(now)
            self._maybe_save(now, monotonic_now)
        except Exception:  # noqa: BLE001
            self.error("Fout bij bijwerken COP-data:\n" + traceback.format_exc())

    def _update_data(self, now, elapsed, monotonic_now):
        self._capture_source_snapshot(now)
        self._update_calendar(now)

        for mode in self.SPACE_MODES:
            self.realtime[mode] = {"electrical": 0.0, "thermal": 0.0}

        operation_state = self._source_values.get(self.entities["operation_state_entity"])
        if not isinstance(operation_state, str) or operation_state.strip().lower() in self.INVALID_STATES:
            # Geen betrouwbare bedrijfstoestand: niets toewijzen en lopende run niet beëindigen.
            self._flush_all()
            self._reset_baselines()
            self._reset_samples()
            self._status = "waiting_for_data"
            return

        mode = next(
            (value for state, value in self.OPERATION_STATES.items() if state in operation_state),
            None,
        )
        dhw_heater = self._source_values.get(self.entities["dhw_heater_entity"]) == "on"
        indoor_power = self._get_float("indoor_power_entity")
        outdoor_power = self._get_float("outdoor_power_entity")
        if outdoor_power is not None:
            outdoor_power = max(0.0, outdoor_power)
        temps = self._select_temperatures(monotonic_now)
        outlet_temp, inlet_temp, source = temps["outlet"], temps["inlet"], temps["source"]
        flow = self._get_float("flow_entity")
        if flow is not None and flow < 0:
            flow = None
        dhw_temp = self._get_float("dhw_current_temp_entity")

        if mode == "dhw":
            loop_outlet, loop_inlet, loop_source = temps["dhw_outlet"], temps["dhw_inlet"], temps["dhw_source"]
        else:
            loop_outlet, loop_inlet, loop_source = outlet_temp, inlet_temp, source
        loop_thermal = self._loop_thermal_power(mode, loop_outlet, loop_inlet, flow, loop_source) if mode else None
        if mode and loop_source and loop_thermal is not None:
            self.data["meta"]["temp_source_seconds"][loop_source] += elapsed

        # Categorie waaronder het elektrische verbruik van deze tick valt.
        if mode in self.SPACE_MODES:
            category = mode if loop_thermal is not None and outdoor_power is not None else "idle"
        elif mode == "dhw":
            category = "dhw"
        else:
            category = "idle"

        indoor_categories = self._indoor_categories(indoor_power, dhw_heater, category)
        outdoor_categories = {category: outdoor_power} if outdoor_power is not None else None
        self._sample_electrical(elapsed, {"outdoor": outdoor_categories, "indoor": indoor_categories})

        # --- Ruimteverwarming / koeling: thermisch + realtime ---
        sample = None
        if mode in self.SPACE_MODES and category == mode:
            pump_power = indoor_categories.get(mode, self.pump_power) if indoor_categories else self.pump_power
            sample = {"mode": mode, "electrical": outdoor_power + pump_power, "thermal": loop_thermal}
            self.realtime[mode] = {"electrical": sample["electrical"], "thermal": sample["thermal"]}
            self._tail = {"mode": mode, "remaining": self.thermal_tail_seconds} if self.thermal_tail_seconds > 0 else None
        elif self._tail is not None:
            sample = self._tail_sample(mode, outlet_temp, inlet_temp, flow, elapsed, source)
        self._integrate_space_thermal(sample, elapsed)

        # --- Warm tapwater, dan tellers verwerken, dan eventueel run afsluiten ---
        self._track_dhw(now, elapsed, mode, dhw_heater, dhw_temp, loop_thermal if mode == "dhw" else None)
        self._settle_all(monotonic_now)
        self._maybe_finish_dhw(now)

        self._track_delta_t_calibration(elapsed, outdoor_power, dhw_heater, flow, temps["own"])
        if mode in self.SPACE_MODES:
            self._track_audit(mode, elapsed, temps["own"], temps["builtin"], flow)
        active = mode is not None or dhw_heater
        self._status = "stale_sources" if (self._stale_sources and active) else "ok"

    def _track_audit(self, mode, elapsed, own_pair, builtin_pair, flow):
        """Tijdgewogen gemiddelde ΔT (incl. offset) van de eigen vs. de ingebouwde sensoren."""
        if elapsed <= 0 or flow is None or flow < self.CAL_MIN_FLOW or own_pair is None or builtin_pair is None:
            return
        own_delta = own_pair[0] - own_pair[1] - self._offset_for("own", own_pair[1])
        builtin_delta = builtin_pair[0] - builtin_pair[1] - self._offset_for("builtin", builtin_pair[1])
        entry = self.data["meta"]["delta_t_audit"].setdefault(mode, {"seconds": 0.0, "own": 0.0, "ref": 0.0})
        entry["seconds"] += elapsed
        entry["own"] += own_delta * elapsed
        entry["ref"] += builtin_delta * elapsed
        if entry["seconds"] > self.AUDIT_WINDOW_SECONDS:
            for key in entry:
                entry[key] *= 0.5
        self._dirty = True

    def _delta_t_audit_summary(self):
        summary = {}
        for mode, entry in self.data["meta"]["delta_t_audit"].items():
            if entry["seconds"] >= self.AUDIT_MIN_SECONDS:
                own = entry["own"] / entry["seconds"]
                ref = entry["ref"] / entry["seconds"]
                summary[mode] = {
                    "hours": round(entry["seconds"] / 3600.0, 1),
                    "own": round(own, 2),
                    "builtin": round(ref, 2),
                    "difference": round(own - ref, 2),
                }
        return summary

    def _tail_sample(self, mode, outlet_temp, inlet_temp, flow, elapsed, source="own"):
        """Uitloop na verwarmen/koelen: warmte die nog vrijkomt terwijl de pomp doorloopt."""
        tail = self._tail
        if mode is not None or flow is None or flow < self.CAL_MIN_FLOW:
            self._tail = None  # andere modus actief of pomp stil: uitloop voorbij
            return None
        thermal = self._loop_thermal_power(tail["mode"], outlet_temp, inlet_temp, flow, source)
        tail["remaining"] -= elapsed
        if thermal is None or thermal <= 0 or tail["remaining"] < 0:
            self._tail = None
            return None
        return {"mode": tail["mode"], "electrical": 0.0, "thermal": thermal, "tail": True}

    def _integrate_space_thermal(self, sample, elapsed):
        hours = elapsed / 3600.0
        previous = self._prev_sample
        for mode in self.SPACE_MODES:
            current = sample if sample and sample["mode"] == mode else None
            before = previous if previous and previous["mode"] == mode else None
            if current is None and before is None:
                continue
            cur_t = current["thermal"] if current else 0.0
            old_t = before["thermal"] if before else 0.0
            thermal_kwh = (old_t + cur_t) / 2.0 * hours / 1000.0
            self._add_energy(mode, 0.0, thermal_kwh)
            if (current or before).get("tail"):
                self.data["meta"]["tail_kwh"][mode] += thermal_kwh
        self._prev_sample = sample

    # ------------------------------------------------------------------
    # DHW-runs
    # ------------------------------------------------------------------
    @staticmethod
    def _new_dhw_run(now, temp):
        return {
            "start_time": now.isoformat(),
            "start_temp": temp,
            "last_temp": temp,
            "ref_temp": temp,
            "rise_sum": 0.0,
            "electrical": 0.0,
            "heater_kwh": 0.0,
            "loop_kwh": 0.0,
            "flow_seconds": 0.0,
            "thermal": 0.0,
            "compressor_seconds": 0.0,
            "inactive_seconds": 0.0,
            "total_seconds": 0.0,
        }

    def _track_dhw_temp(self, run, temp):
        """Tel alleen stijgingen groter dan de deadband (filtert sensorruis)."""
        run["last_temp"] = temp
        if temp >= run["ref_temp"] + self.dhw_temp_deadband:
            run["rise_sum"] += temp - run["ref_temp"]
            run["ref_temp"] = temp
        elif temp <= run["ref_temp"] - self.dhw_temp_deadband:
            run["ref_temp"] = temp

    def _track_dhw(self, now, elapsed, mode, dhw_heater, dhw_temp, loop_thermal):
        dhw = self.data["dhw"]
        run = dhw["current_run"]
        active = mode == "dhw" or dhw_heater

        if run is None:
            if not (active and dhw_temp is not None):
                self._prev_dhw_thermal = None
                return
            run = self._new_dhw_run(now, dhw_temp)
            dhw["current_run"] = run

        self._dirty = True
        run["total_seconds"] += elapsed
        if dhw_temp is not None:
            self._track_dhw_temp(run, dhw_temp)
        if active:
            run["inactive_seconds"] = 0.0
            if mode == "dhw":
                run["compressor_seconds"] += elapsed
        else:
            run["inactive_seconds"] += elapsed

        # Onafhankelijke thermische schatting: debiet x ΔT tijdens compressor-DHW.
        previous = self._prev_dhw_thermal
        if loop_thermal is not None or previous is not None:
            run["loop_kwh"] += ((previous or 0.0) + (loop_thermal or 0.0)) / 2.0 * (elapsed / 3600.0) / 1000.0
            if loop_thermal is not None:
                run["flow_seconds"] += elapsed
        self._prev_dhw_thermal = loop_thermal

    def _maybe_finish_dhw(self, now):
        run = self.data["dhw"]["current_run"]
        if run is None:
            return
        if run["inactive_seconds"] >= self.dhw_end_grace or run["total_seconds"] >= self.dhw_max_run_seconds:
            # Nog niet toegewezen DHW-/elementenergie ongeschaald meenemen (klein bedrag).
            self._force_flush(("dhw", "element"))
            self._finalize_dhw_run(run, now)

    def _finalize_dhw_run(self, run, now):
        electrical = run["electrical"]
        heater = run["heater_kwh"]
        rise = run["rise_sum"] + max(0.0, run["last_temp"] - run["ref_temp"])
        tank_total = rise * self.dhw_tank_volume * SPECIFIC_HEAT_WATER / JOULES_PER_KWH
        flow_valid = (
            run["compressor_seconds"] > 0
            and run["flow_seconds"] >= self.DHW_FLOW_COVERAGE * run["compressor_seconds"]
        )
        flow_total = max(0.0, run["loop_kwh"]) + heater if flow_valid else None

        if run["compressor_seconds"] <= 0:
            thermal, method = electrical, "element"  # alleen elektrisch element: COP 1
        elif self.dhw_thermal_method == "flow" and flow_total is not None:
            thermal, method = flow_total, "flow"
        else:
            thermal, method = max(tank_total, heater), "tank"  # element-warmte is zeker geleverd

        run["thermal"] = thermal
        run["thermal_method"] = method
        run["thermal_tank"] = round(tank_total, 4)
        run["thermal_flow"] = round(flow_total, 4) if flow_total is not None else None
        run["flow_vs_tank"] = round(flow_total / tank_total, 2) if flow_total is not None and tank_total > 0.05 else None
        run["end_time"] = now.isoformat()
        run["end_temp"] = run["last_temp"]
        run["duration_minutes"] = round(max(0.0, run["total_seconds"] - run["inactive_seconds"]) / 60.0, 1)

        self._add_energy("dhw", electrical, thermal)
        runs = self.data["dhw"]["runs"]
        runs.append(run)
        del runs[: -self.max_stored_dhw_runs]
        self.data["dhw"]["current_run"] = None
        self._dirty = True
        self._force_save = True

    # ------------------------------------------------------------------
    # Sensoren publiceren
    # ------------------------------------------------------------------
    def _set_sensor(self, entity_id, state, attributes):
        monotonic_now = time.monotonic()
        fingerprint = json.dumps([state, attributes], sort_keys=True, default=str)
        cached = self._published.get(entity_id)
        if cached and cached[0] == fingerprint and monotonic_now - cached[1] < self.REPUBLISH_SECONDS:
            return
        self.set_state(entity_id, state=state, attributes=attributes)
        self._published[entity_id] = (fingerprint, monotonic_now)

    def _publish_sensors(self, now):
        for mode in self.SPACE_MODES:
            power = self.realtime[mode]
            self._set_sensor(
                f"sensor.hitachi_yutaki_{mode}_realtime_cop",
                self._cop(power["thermal"], power["electrical"]),
                {
                    "friendly_name": f"Hitachi Yutaki {self.MODE_LABELS[mode]} Realtime COP",
                    "state_class": "measurement",
                    "icon": "mdi:heat-pump",
                    "electrical_power": round(power["electrical"], 1),
                    "thermal_power": round(power["thermal"], 1),
                    # oude attribuutnamen (waren eigenlijk watt), behouden voor compatibiliteit
                    "electrical_energy": round(power["electrical"], 1),
                    "thermal_energy": round(power["thermal"], 1),
                },
            )

        self._publish_dhw_run_sensor()

        for period in self.PERIODS:
            period_start = None if period == "lifetime" else self._period_start_iso(period, now)
            for mode in self.MODES:
                energy = self.data[period][mode]["energy"]
                electrical, thermal = energy["electrical"], energy["thermal"]
                label = f"Hitachi Yutaki {self.MODE_LABELS[mode]} {self.PERIOD_LABELS[period]}"
                self._set_sensor(
                    f"sensor.hitachi_yutaki_{mode}_{period}_cop",
                    self._cop(thermal, electrical),
                    {
                        "friendly_name": f"{label} COP",
                        "state_class": "measurement",
                        "icon": "mdi:heat-pump",
                        "electrical_energy": round(electrical, 4),
                        "thermal_energy": round(thermal, 4),
                    },
                )
                for kind, value in (("thermal", thermal), ("electrical", electrical)):
                    attributes = {
                        "friendly_name": f"{label} {kind.title()} Energy",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "icon": "mdi:lightning-bolt" if kind == "electrical" else "mdi:fire",
                    }
                    if period == "lifetime":
                        attributes["state_class"] = "total_increasing" if kind == "electrical" else "total"
                    else:
                        attributes["state_class"] = "total"
                        attributes["last_reset"] = period_start
                    self._set_sensor(
                        f"sensor.hitachi_yutaki_{mode}_{period}_{kind}_energy",
                        round(value, 3),
                        attributes,
                    )

        self._publish_status()

    def _publish_dhw_run_sensor(self):
        runs = self.data["dhw"]["runs"]
        attributes = {
            "friendly_name": "Hitachi Yutaki DHW Run COP",
            "state_class": "measurement",
            "icon": "mdi:water-boiler",
        }
        state = "unknown"
        if runs:
            last_run = runs[-1]
            electrical = self._num(last_run.get("electrical"))
            thermal = self._num(last_run.get("thermal"))
            if electrical >= self.min_run_electrical_kwh:
                state = self._cop(thermal, electrical)
            attributes.update(
                {
                    "start_time": last_run.get("start_time"),
                    "end_time": last_run.get("end_time"),
                    "start_temp": last_run.get("start_temp"),
                    "end_temp": last_run.get("end_temp", last_run.get("last_temp")),
                    "duration_minutes": last_run.get("duration_minutes"),
                    "electrical_energy": round(electrical, 4),
                    "thermal_energy": round(thermal, 4),
                    "heater_energy": round(self._num(last_run.get("heater_kwh")), 4),
                    "thermal_method": last_run.get("thermal_method"),
                    "thermal_tank": last_run.get("thermal_tank"),
                    "thermal_flow": last_run.get("thermal_flow"),
                    "flow_vs_tank": last_run.get("flow_vs_tank"),
                }
            )
        self._set_sensor("sensor.hitachi_yutaki_dhw_run_cop", state, attributes)

    def _publish_status(self):
        calibration = self._delta_t_calibration()
        electrical_source, counter_vs_sampled = {}, {}
        for meter in self.METERS:
            track = self._counters[meter]
            counting = self.use_energy_counters and track["last"] is not None
            electrical_source[meter] = "counter" if counting else "sampled"
            counter_vs_sampled[meter] = (
                round(track["counter_kwh"] / track["sampled_kwh"], 2) if track["sampled_kwh"] > 0.2 else None
            )
        self._set_sensor(
            self.STATUS_ENTITY,
            self._status,
            {
                "friendly_name": "Hitachi Yutaki COP Calculator Status",
                "icon": "mdi:heart-pulse",
                "stale_sources": self._stale_sources,
                "dhw_run_active": self.data["dhw"]["current_run"] is not None,
                "last_saved": self._last_saved_iso,
                "last_backup": self._last_backup_iso,
                "tracking_since": self.data["meta"].get("lifetime_start"),
                "thermal_tail_kwh": {m: round(v, 3) for m, v in self.data["meta"]["tail_kwh"].items()},
                "electrical_source": electrical_source,
                "counter_vs_sampled": counter_vs_sampled,
                "temperature_source": self._temp_info["setting"],
                "temperature_source_in_use": self._source_in_use,
                "own_sensors_deviating": self._temp_info["deviating"],
                "temperature_source_hours": {
                    key: round(value / 3600.0, 1) for key, value in self.data["meta"]["temp_source_seconds"].items()
                },
                "builtin_delta_t_offset_applied": self.builtin_delta_t_offset,
                "delta_t_offset_applied": self.delta_t_offset_curve or self.delta_t_offset,
                "delta_t_suggested_offset": calibration["offset"],
                "delta_t_calibration_samples": calibration["samples"],
                "delta_t_calibration_spread": calibration["spread"],
                "delta_t_offset_by_inlet_temp": calibration["by_temp"],
                "delta_t_suggested_curve": calibration["curve"],
                "delta_t_audit": self._delta_t_audit_summary(),
                "storage_file": self.storage_file,
            },
        )
