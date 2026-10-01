import datetime
import json
import os
import statistics
import time

import hassapi as hass


class COPCalculator(hass.Hass):
    UPDATE_INTERVAL = 30
    PUMP_POWER = 59
    DHW_TANK_VOLUME = 260
    STORAGE_FILE = "cop_calculator_data.json"
    MAX_STORED_DHW_RUNS = 1
    MIN_STALE_AFTER_SECONDS = 120
    UNKNOWN_FREQUENCY_STALE_AFTER_SECONDS = 300
    STALE_INTERVAL_MULTIPLIER = 3
    MAX_FREQUENCY_SAMPLES = 20

    ENTITIES = {
        "indoor_power_entity": "sensor.shelly_warmtepomp_binnenunit_active_power",
        "outdoor_power_entity": "sensor.shelly_warmtepomp_buitenunit_active_power",
        "dhw_heater_entity": "binary_sensor.control_unit_dhw_heater_2",
        "outlet_temp_entity": "sensor.control_unit_water_outlet_temperature_2",
        "inlet_temp_entity": "sensor.control_unit_water_inlet_temperature_2",
        "flow_entity": "sensor.control_unit_water_flow_2",
        "operation_state_entity": "sensor.control_unit_operation_state_2",
        "dhw_current_temp_entity": "sensor.dhw_current_temperature",
        "dhw_target_temp_entity": "sensor.dhw_temperatuur_set_corrected",
    }
    SOURCE_ENTITY_KEYS = (
        "indoor_power_entity",
        "outdoor_power_entity",
        "dhw_heater_entity",
        "outlet_temp_entity",
        "inlet_temp_entity",
        "flow_entity",
        "operation_state_entity",
        "dhw_current_temp_entity",
    )

    MODES = ("heating", "cooling", "dhw")
    PERIODS = ("daily", "monthly", "yearly", "lifetime")
    OPERATION_STATES = {
        "operation_state_heat_thermo_on": "heating",
        "operation_state_cool_thermo_on": "cooling",
        "operation_state_dhw_on": "dhw",
    }

    def initialize(self):
        for key, default in self.ENTITIES.items():
            setattr(self, key, default)

        self.pump_power = float(self.PUMP_POWER)
        self.dhw_tank_volume = float(self.DHW_TANK_VOLUME)
        self.update_interval = int(self.UPDATE_INTERVAL)
        storage_file = self.STORAGE_FILE
        if not os.path.isabs(storage_file):
            storage_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), storage_file)
        self.storage_file = storage_file

        self.data = self._default_data()
        self._load_data()
        self._source_values = {}
        self._source_freshness = {}
        self._source_update_history = {}
        self._initialize_calendar()
        self._last_update_monotonic = time.monotonic()
        self._publish_sensors()
        self.run_every(self._update, "now", self.update_interval)
        self.log("COP Calculator gestart.")

    def _default_data(self):
        data = {}
        for mode in self.MODES:
            mode_data = {
                "power": {"electrical": 0, "thermal": 0},
                "energy": {"electrical": 0, "thermal": 0},
                "runs": [] if mode == "dhw" else None,
                "last_energy_thermal": 0,
                "last_energy_electrical": 0,
            }
            if mode == "dhw":
                mode_data["current_run"] = None
            data[mode] = mode_data
        for period in self.PERIODS:
            data[period] = {
                mode: {"energy": {"electrical": 0, "thermal": 0}}
                for mode in self.MODES
            }
        data["calendar"] = {"last_day": None, "last_month": None, "last_year": None}
        return data

    def _merge_data(self, target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._merge_data(target[key], value)
            else:
                target[key] = value

    def _load_data(self):
        try:
            with open(self.storage_file, "r", encoding="utf-8") as data_file:
                saved_data = json.load(data_file)
            if isinstance(saved_data, dict):
                self._merge_data(self.data, saved_data)
            runs = self.data["dhw"].get("runs")
            if not isinstance(runs, list):
                runs = []
            self.data["dhw"]["runs"] = runs[-self.MAX_STORED_DHW_RUNS:]
            if not isinstance(self.data["dhw"].get("current_run"), dict):
                self.data["dhw"]["current_run"] = None
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as error:
            self.warning(f"Persistente COP-data kon niet worden geladen: {error}")

    def _save_data(self):
        temporary_file = f"{self.storage_file}.tmp"
        try:
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            with open(temporary_file, "w", encoding="utf-8") as data_file:
                json.dump(self.data, data_file, ensure_ascii=True)
            os.replace(temporary_file, self.storage_file)
        except OSError as error:
            self.error(f"Persistente COP-data kon niet worden opgeslagen: {error}")
            try:
                os.remove(temporary_file)
            except OSError:
                pass

    def _initialize_calendar(self):
        now = datetime.datetime.now().astimezone()
        calendar = self.data["calendar"]
        if calendar["last_day"] is None:
            calendar["last_day"] = now.date().isoformat()
        if calendar["last_month"] is None:
            calendar["last_month"] = f"{now.year}-{now.month:02d}"
        if calendar["last_year"] is None:
            calendar["last_year"] = str(now.year)

    def _get_float(self, entity_id):
        value = self._source_values.get(entity_id)
        if entity_id not in self._source_values:
            value = self.get_state(entity_id)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _get_state_snapshot(self, entity_id):
        try:
            state = self.get_state(entity_id, attribute="all")
        except TypeError:
            state = self.get_state(entity_id)
        if isinstance(state, dict) and "state" in state:
            return state.get("state"), state.get("last_reported") or state.get("last_updated")
        return state, None

    def _parse_update_time(self, value):
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
        now_utc = now.astimezone(datetime.timezone.utc)
        source_values = {}
        source_freshness = {}
        for key in self.SOURCE_ENTITY_KEYS:
            entity_id = self.ENTITIES[key]
            state, last_updated = self._get_state_snapshot(entity_id)
            source_values[entity_id] = state
            updated_at = self._parse_update_time(last_updated)
            history = self._source_update_history.setdefault(
                entity_id, {"last_updated": None, "intervals": []}
            )

            if updated_at is not None:
                updated_iso = updated_at.isoformat()
                previous_updated = self._parse_update_time(history["last_updated"])
                if previous_updated is not None and updated_at > previous_updated:
                    interval = (updated_at - previous_updated).total_seconds()
                    if interval > 0:
                        history["intervals"].append(interval)
                        del history["intervals"][:-self.MAX_FREQUENCY_SAMPLES]
                history["last_updated"] = updated_iso
                age_seconds = max(0, (now_utc - updated_at).total_seconds())
            else:
                age_seconds = None

            intervals = history["intervals"]
            typical_interval = statistics.median(intervals) if intervals else None
            stale_after = (
                max(self.MIN_STALE_AFTER_SECONDS, typical_interval * self.STALE_INTERVAL_MULTIPLIER)
                if typical_interval is not None
                else self.UNKNOWN_FREQUENCY_STALE_AFTER_SECONDS
            )
            source_freshness[entity_id] = {
                "age_seconds": age_seconds,
                "typical_update_seconds": typical_interval,
                "stale_after_seconds": stale_after,
                "stale": age_seconds is not None and age_seconds > stale_after,
            }

        self._source_values = source_values
        self._source_freshness = source_freshness

    def _update_calendar(self, now):
        calendar = self.data["calendar"]
        current_day = now.date().isoformat()
        current_month = f"{now.year}-{now.month:02d}"
        current_year = str(now.year)
        reset_periods = []

        for period, current_value in (
            ("daily", current_day),
            ("monthly", current_month),
            ("yearly", current_year),
        ):
            key = {"daily": "last_day", "monthly": "last_month", "yearly": "last_year"}[period]
            if calendar[key] != current_value:
                for mode in self.MODES:
                    self.data[period][mode]["energy"] = {"electrical": 0, "thermal": 0}
                calendar[key] = current_value
                reset_periods.append(period)

        if reset_periods:
            self.log(f"COP-tellers gereset voor: {', '.join(reset_periods)}.")

    def _update(self, kwargs):
        try:
            now = datetime.datetime.now().astimezone()
            monotonic_now = time.monotonic()
            elapsed_seconds = max(0, monotonic_now - self._last_update_monotonic)
            self._last_update_monotonic = monotonic_now
            self._update_data(now, elapsed_seconds)
            self._publish_sensors()
            self._save_data()
        except Exception as error:
            self.error(f"Fout bij bijwerken COP-data: {error}")

    def _update_data(self, now, elapsed_seconds):
        self._capture_source_snapshot(now)
        self._update_calendar(now)

        indoor_power = self._get_float(self.indoor_power_entity)
        outdoor_power = self._get_float(self.outdoor_power_entity)
        dhw_heater = self._source_values.get(self.dhw_heater_entity) == "on"
        outlet_temp = self._get_float(self.outlet_temp_entity)
        inlet_temp = self._get_float(self.inlet_temp_entity)
        flow = self._get_float(self.flow_entity)
        operation_state = self._source_values.get(self.operation_state_entity) or ""
        dhw_current_temp = self._get_float(self.dhw_current_temp_entity)
        mode = next(
            (value for state, value in self.OPERATION_STATES.items() if state in operation_state),
            None,
        )

        if mode in ("heating", "cooling") and None not in (outdoor_power, outlet_temp, inlet_temp, flow):
            flow_kg_s = flow * 1000 / 3600
            delta_temp = outlet_temp - inlet_temp if mode == "heating" else inlet_temp - outlet_temp
            thermal_power = flow_kg_s * 4180 * delta_temp if delta_temp > 0 else 0
            electrical_power = outdoor_power + self.pump_power
            mode_data = self.data[mode]
            mode_data["power"]["thermal"] = thermal_power
            mode_data["power"]["electrical"] = electrical_power

            if thermal_power > 0:
                interval_hours = elapsed_seconds / 3600
                mode_data["energy"]["thermal"] += thermal_power * interval_hours / 1000
                mode_data["energy"]["electrical"] += electrical_power * interval_hours / 1000

        dhw_active = mode == "dhw" or dhw_heater
        current_dhw_run = self.data["dhw"]["current_run"]
        if dhw_active and current_dhw_run is None and dhw_current_temp is not None:
            current_dhw_run = {
                "start_time": now.isoformat(),
                "start_temp": dhw_current_temp,
                "last_temp": dhw_current_temp,
                "electrical": 0,
                "thermal": 0,
                "sum_of_drops": 0,
            }
            self.data["dhw"]["current_run"] = current_dhw_run

        if current_dhw_run is not None and dhw_current_temp is not None:
            run = current_dhw_run
            delta_temp = dhw_current_temp - run["last_temp"]
            if delta_temp < 0:
                run["sum_of_drops"] += abs(delta_temp)
            run["last_temp"] = dhw_current_temp

            interval_hours = elapsed_seconds / 3600
            if dhw_heater and indoor_power is not None:
                run["electrical"] += (indoor_power - self.pump_power) * interval_hours / 1000
            if mode == "dhw" and outdoor_power is not None:
                run["electrical"] += (outdoor_power + self.pump_power) * interval_hours / 1000

        if current_dhw_run is not None and not dhw_active:
            run = current_dhw_run
            thermal_delta = (run["last_temp"] - run["start_temp"]) + run["sum_of_drops"]
            electrical_increment = run["electrical"]
            if dhw_heater:
                thermal_increment = electrical_increment
            else:
                thermal_increment = thermal_delta * self.dhw_tank_volume * 4180 / 3600000

            self.data["dhw"]["energy"]["thermal"] += thermal_increment
            self.data["dhw"]["energy"]["electrical"] += electrical_increment
            for period in self.PERIODS:
                energy = self.data[period]["dhw"]["energy"]
                energy["thermal"] += thermal_increment
                energy["electrical"] += electrical_increment

            run["thermal"] = thermal_increment
            runs = self.data["dhw"]["runs"]
            runs.append(run)
            del runs[:-self.MAX_STORED_DHW_RUNS]
            self.data["dhw"]["last_energy_thermal"] = self.data["dhw"]["energy"]["thermal"]
            self.data["dhw"]["last_energy_electrical"] = self.data["dhw"]["energy"]["electrical"]
            self.data["dhw"]["current_run"] = None

        if mode in ("heating", "cooling"):
            mode_data = self.data[mode]
            thermal_delta = mode_data["energy"]["thermal"] - mode_data["last_energy_thermal"]
            electrical_delta = mode_data["energy"]["electrical"] - mode_data["last_energy_electrical"]
            for period in self.PERIODS:
                energy = self.data[period][mode]["energy"]
                energy["thermal"] += thermal_delta
                energy["electrical"] += electrical_delta
            mode_data["last_energy_thermal"] = mode_data["energy"]["thermal"]
            mode_data["last_energy_electrical"] = mode_data["energy"]["electrical"]

    def _publish_sensors(self):
        for mode in ("heating", "cooling"):
            self._publish_cop_sensor(mode, "realtime")
        self._publish_dhw_run_sensor()
        for mode in self.MODES:
            for period in self.PERIODS:
                self._publish_cop_sensor(mode, period)

    def _publish_cop_sensor(self, mode, period):
        if period == "realtime":
            values = self.data[mode]["power"]
        else:
            values = self.data[period][mode]["energy"]
        electrical = values["electrical"]
        thermal = values["thermal"]
        state = round(thermal / electrical, 2) if electrical > 0 else "unknown"
        name = f"Hitachi Yutaki {mode.title()} {period.title()} COP"
        self.set_state(
            f"sensor.hitachi_yutaki_{mode}_{period}_cop",
            state=state,
            attributes={
                "friendly_name": name,
                "state_class": "measurement",
                "electrical_energy": electrical,
                "thermal_energy": thermal,
                "source_freshness": self._source_freshness,
            },
        )

    def _publish_dhw_run_sensor(self):
        runs = self.data["dhw"]["runs"]
        attributes = {"friendly_name": "Hitachi Yutaki DHW Run COP", "state_class": "measurement"}
        state = "unknown"
        if runs:
            last_run = runs[-1]
            electrical = last_run["electrical"]
            thermal = last_run["thermal"]
            state = round(thermal / electrical, 2) if electrical > 0 else "unknown"
            attributes.update(
                {
                    "start_time": last_run["start_time"],
                    "start_temp": last_run["start_temp"],
                    "electrical_energy": electrical,
                    "thermal_energy": thermal,
                }
            )
        self.set_state("sensor.hitachi_yutaki_dhw_run_cop", state=state, attributes=attributes)