import datetime
import json
import sys
import threading
import types
import unittest

try:
    import hassapi  # noqa: F401
except ImportError:
    hassapi_stub = types.ModuleType("hassapi")
    hassapi_stub.Hass = object
    sys.modules["hassapi"] = hassapi_stub

from dateutil import tz

from appdaemon_laadpaal.peblarhorizonplanner import PeblarHorizonPlanner


class PeblarHorizonPlannerTests(unittest.TestCase):
    def make_planner(self):
        planner = object.__new__(PeblarHorizonPlanner)
        planner._lock = threading.RLock()
        return planner

    def test_plan_uses_gross_energy_after_charging_efficiency(self):
        planner = self.make_planner()
        for entity_key, entity_id in planner.DEFAULT_ENTITIES.items():
            setattr(planner, entity_key, entity_id)
        planner._local_tz = tz.gettz("Europe/Amsterdam")
        planner._forecast_alarm_sent = False
        planner._forecast_status = None
        planner._tarief_meta = {}
        planner._gelogde_meldingen = {}
        planner.plan = {}
        planner.matrix_kwartieren = []
        planner.last_plan_calculation = None
        planner.plan_valid_until = None
        planner._wacht_op_forecast = False
        planner.planning_geannuleerd = False
        planner.initiele_energie = None
        planner.cumulatief_geladen_kwh = 0.0
        planner.sessie_actief = False
        planner.sessie_doel_kwh = 0.0
        planner.doel_soc = None
        planner.vorige_soc = None
        planner.vorige_soc_energie = None
        planner._plan_status = "geen_plan"
        planner._set_desired_state = lambda *args: None
        planner._set_plan_status = lambda status, message=None: setattr(planner, "_plan_status", status)
        planner._save_persistent_data = lambda: None
        planner._update_graph_data = lambda: None
        planner._send_push_notification = lambda *args: None
        planner.log = lambda *args, **kwargs: None
        planner.warning = lambda *args, **kwargs: None
        planner.error = lambda *args, **kwargs: None

        now = datetime.datetime.now(tz.UTC)
        departure = (now + datetime.timedelta(hours=6)).astimezone(planner._local_tz)
        slot_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        forecast = []
        while slot_start <= now + datetime.timedelta(hours=6):
            forecast.append({
                "tijd": slot_start.isoformat(),
                "pv_kw": 0.0,
                "verbruik_kw": 0.11,
                "epex_prijs": 0.2,
                "net_prijs": 0.46,
                "prijs_bron": "epex",
            })
            slot_start += datetime.timedelta(minutes=15)

        states = {
            planner.soc_entity: "50",
            planner.bereik_entity: "200",
            planner.gewenste_km_entity: "300",
            planner.peblar_energie_entity: "1000",
            planner.peblar_status_entity: "charging",
            planner.peblar_switch_entity: "off",
            planner.peblar_mode_entity: "Pure solar",
            planner.vertrektijd_entity: {
                "state": departure.strftime("%Y-%m-%d %H:%M:%S"),
                "attributes": {"has_date": True, "has_time": True},
            },
            planner.forecast_entity: {
                "state": "ready",
                "attributes": {
                    "generated_at": now.isoformat(),
                    "forecast": forecast,
                },
            },
        }

        def get_state(entity_id, attribute=None):
            value = states.get(entity_id)
            if attribute == "all":
                if isinstance(value, dict):
                    return value
                return {"state": value, "attributes": {}}
            if isinstance(value, dict):
                return value.get("state")
            return value

        planner.get_state = get_state
        planner._check_kritieke_sensoren = lambda **kwargs: True
        planner._is_auto_verbonden = lambda: True

        planner.bereken_laadplan()

        self.assertAlmostEqual(planner.sessie_doel_kwh, 30.8 / planner.LAADRENDEMENT)
        self.assertAlmostEqual(
            sum(entry["zon_kwh"] + entry["net_kwh"] for entry in planner.plan.values()),
            planner.sessie_doel_kwh,
            places=2,
        )
        planner.initiele_energie = 995.0
        planner.cumulatief_geladen_kwh = 5.0
        states[planner.soc_entity] = "55"
        states[planner.bereik_entity] = "220"
        states[planner.peblar_energie_entity] = "1010"
        planner.bereken_laadplan(concept=False)
        self.assertEqual(planner.initiele_energie, 1010.0)
        self.assertEqual(planner.cumulatief_geladen_kwh, 0.0)
        self.assertAlmostEqual(planner.sessie_doel_kwh, 26.95 / planner.LAADRENDEMENT)

    def test_fallback_planner_does_not_keep_previous_partial_plan(self):
        planner = self.make_planner()
        planner.plan = {
            "old": {"zon_kwh": 2.0, "net_kwh": 0.0},
        }
        planner.matrix_kwartieren = [
            {"tijd": "first", "net_prijs": 0.1, "max_net_kwh": 1.0, "laadbare_zon_kwh": 0.0},
            {"tijd": "second", "net_prijs": 0.2, "max_net_kwh": 1.0, "laadbare_zon_kwh": 0.0},
        ]

        planner._plan_zo_veel_mogelijk(1.0)

        self.assertNotIn("old", planner.plan)
        self.assertAlmostEqual(
            sum(entry["zon_kwh"] + entry["net_kwh"] for entry in planner.plan.values()),
            1.0,
        )

    def test_solar_threshold_is_independent_of_partial_slot_duration(self):
        planner = self.make_planner()
        self.assertFalse(planner._zonoverschot_bruikbaar(1.37, 0.1))
        self.assertTrue(planner._zonoverschot_bruikbaar(1.38, 0.1))

    def test_ampere_conversion_respects_phase_count(self):
        planner = self.make_planner()
        planner.FASEN = 1
        planner.SPANNING_V = 230.0
        self.assertAlmostEqual(planner._kw_naar_ampere(2.3), 10.0)
        planner.FASEN = 3
        self.assertAlmostEqual(planner._kw_naar_ampere(6.9), 10.0)

    def test_fordpass_sensors_are_not_critical_during_execution(self):
        planner = self.make_planner()
        planner.peblar_energie_entity = "energy"
        planner.peblar_status_entity = "status"
        planner.peblar_switch_entity = "switch"
        planner.peblar_mode_entity = "mode"
        planner.soc_entity = "soc"
        planner.bereik_entity = "range"
        states = {
            "energy": "100",
            "status": "charging",
            "switch": "on",
            "mode": "Default",
            "soc": "unavailable",
            "range": "unknown",
        }
        planner.get_state = lambda entity: states[entity]
        planner.error = lambda *args, **kwargs: None
        planner._send_push_notification = lambda *args: None

        self.assertTrue(planner._check_kritieke_sensoren(include_vehicle_sensors=False))
        self.assertFalse(planner._check_kritieke_sensoren())

    def test_unavailable_fordpass_does_not_stop_active_execution(self):
        planner = self.make_planner()
        for entity_key, entity_id in planner.DEFAULT_ENTITIES.items():
            setattr(planner, entity_key, entity_id)
        planner._plan_status = "in_uitvoering"
        planner.PLAN_UITVOEREN_BOOLEAN = "execution"
        planner.planning_geannuleerd = False
        planner.matrix_kwartieren = [{
            "tijd": datetime.datetime.now(tz.UTC).replace(
                minute=(datetime.datetime.now(tz.UTC).minute // 15) * 15,
                second=0,
                microsecond=0,
            ),
            "duration_hours": 0.25,
            "laadbare_zon_kwh": 0.5,
        }]
        planner.plan = {
            planner.matrix_kwartieren[0]["tijd"]: {"zon_kwh": 0.5, "net_kwh": 0.0},
        }
        planner.plan_valid_until = datetime.datetime.now(tz.UTC) + datetime.timedelta(hours=1)
        planner.initiele_energie = 100.0
        planner.cumulatief_geladen_kwh = 0.0
        planner.sessie_doel_kwh = 20.0
        planner.sessie_actief = True
        planner.vorige_soc = None
        planner.vorige_soc_energie = None
        planner.last_power_on_time = None
        planner.last_power_off_time = None
        planner._set_execution_boolean_calls = []
        planner._desired_state_calls = []
        planner._set_desired_state = lambda *args: None
        planner._set_execution_boolean = lambda enabled: planner._set_execution_boolean_calls.append(enabled)
        planner._set_state_respecting_minimum_off = lambda *args: planner._desired_state_calls.append(args)
        planner._set_plan_status = lambda status, message=None: setattr(planner, "_plan_status", status)
        planner._plan_afwijking_kwh = lambda *args: None
        planner._is_auto_verbonden = lambda: True
        planner._save_persistent_data = lambda: None
        planner._send_push_notification = lambda *args: None
        planner.error = lambda *args, **kwargs: None
        planner.log = lambda *args, **kwargs: None
        planner._log_bij_verandering = lambda *args: None
        states = {
            planner.PLAN_UITVOEREN_BOOLEAN: "on",
            planner.soc_entity: "unavailable",
            planner.peblar_energie_entity: "100",
            planner.peblar_status_entity: "suspendedev",
            planner.peblar_switch_entity: "off",
            planner.peblar_mode_entity: "Pure solar",
        }
        planner.get_state = lambda entity, attribute=None: states.get(entity)
        planner._check_kritieke_sensoren = PeblarHorizonPlanner._check_kritieke_sensoren.__get__(planner)

        planner.voer_schakeling_uit()

        self.assertEqual(planner._plan_status, "in_uitvoering")
        self.assertEqual(planner._set_execution_boolean_calls, [])
        self.assertEqual(planner._desired_state_calls, [("Pure solar", True, 2.0)])

    def test_suspended_zero_power_status_keeps_session_active(self):
        planner = self.make_planner()
        planner.planning_geannuleerd = False
        planner.sessie_actief = True
        planner._plan_status = "in_uitvoering"
        planner.soc_entity = "soc"
        planner._set_execution_boolean_calls = []
        planner._reset_calls = []
        planner._clear_plan_calls = []
        planner._set_execution_boolean = lambda enabled: planner._set_execution_boolean_calls.append(enabled)
        planner._set_desired_state = lambda *args: planner.fail("Suspended should not cause a control change.")
        planner._reset_laadtracking = lambda: planner._reset_calls.append(True)
        planner._clear_plan = lambda: planner._clear_plan_calls.append(True)
        planner._set_plan_status = lambda status, message=None: setattr(planner, "_plan_status", status)
        planner._save_persistent_data = lambda: None
        planner._send_push_notification = lambda *args: None
        planner._log_bij_verandering = lambda *args: None
        planner.log = lambda *args, **kwargs: None
        planner.get_sensor_float = lambda entity: 50.0

        planner._auto_status_changed("status", None, "charging", "suspendedev", {})

        self.assertEqual(planner._plan_status, "in_uitvoering")
        self.assertEqual(planner._set_execution_boolean_calls, [])
        self.assertEqual(planner._reset_calls, [])
        self.assertEqual(planner._clear_plan_calls, [])

    def test_unknown_peblar_status_does_not_reset_active_session(self):
        planner = self.make_planner()
        planner.planning_geannuleerd = False
        planner._plan_status = "in_uitvoering"
        planner._set_execution_boolean_calls = []
        planner._reset_calls = []
        planner._clear_plan_calls = []
        planner._set_execution_boolean = lambda enabled: planner._set_execution_boolean_calls.append(enabled)
        planner._set_desired_state = lambda *args: planner.fail("Unknown should not cause a control change.")
        planner._reset_laadtracking = lambda: planner._reset_calls.append(True)
        planner._clear_plan = lambda: planner._clear_plan_calls.append(True)
        planner._set_plan_status = lambda status, message=None: setattr(planner, "_plan_status", status)
        planner._log_bij_verandering = lambda *args: None

        planner._auto_status_changed("status", None, "charging", "unavailable", {})

        self.assertEqual(planner._plan_status, "in_uitvoering")
        self.assertEqual(planner._set_execution_boolean_calls, [])
        self.assertEqual(planner._reset_calls, [])
        self.assertEqual(planner._clear_plan_calls, [])

    def test_soc_jump_is_checked_against_charger_meter_energy(self):
        planner = self.make_planner()
        planner.vorige_soc = 50.0
        planner.vorige_soc_energie = 100.0
        planner._log_bij_verandering = lambda *args: None

        self.assertTrue(planner._soc_update_is_plausible(60.0, 100.0 + (10.0 * 77.0 / 90.0)))
        self.assertFalse(planner._soc_update_is_plausible(80.0, 100.0 + (10.0 * 77.0 / 90.0) + 1.0))
        self.assertEqual(planner.vorige_soc, 60.0)

    def test_plan_deviation_uses_elapsed_fraction_of_quarter(self):
        planner = self.make_planner()
        start = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=tz.UTC)
        planner._plan_start_time = start
        planner._plan_start_meterstand = 100.0
        planner.matrix_kwartieren = [{
            "tijd": start,
            "duration_hours": 0.25,
        }]
        planner.plan = {
            start: {"zon_kwh": 2.0, "net_kwh": 0.0},
        }

        deviation = planner._plan_afwijking_kwh(
            101.25,
            start + datetime.timedelta(minutes=7, seconds=30),
        )

        self.assertAlmostEqual(deviation, 0.25)

    def test_error_sanitizer_preserves_ordinary_numbers_and_redacts_secrets(self):
        planner = self.make_planner()
        sanitized = planner._sanitize_error(Exception("power 10.0 kW at 192.168.1.20 token=abc123"))
        self.assertIn("10.0 kW", sanitized)
        self.assertNotIn("192.168.1.20", sanitized)
        self.assertNotIn("abc123", sanitized)

    def test_graph_topics_keep_full_horizon_across_small_pages(self):
        planner = self.make_planner()
        planner.GRAPH_DATA_ENTITY = "sensor.test_graph"
        planner.GRAPH_ATTRIBUTE_MAX_BYTES = 1200
        planner.set_state = lambda entity, state, attributes: recorded.setdefault(entity, attributes)
        recorded = {}
        times = [f"2026-10-06 {index // 4:02d}:{(index % 4) * 15:02d}" for index in range(1000)]
        prices = [round(index / 100, 2) for index in range(1000)]

        entities = planner._publish_graph_topic(
            "prijzen",
            times,
            {"totaal_tarieven": prices},
            datetime.datetime.now(tz.UTC).isoformat(),
        )

        pages = [recorded[entity] for entity in entities]
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(
            len(json.dumps(page, separators=(",", ":")).encode("utf-8")) <= planner.GRAPH_ATTRIBUTE_MAX_BYTES
            for page in pages
        ))
        self.assertEqual(sum(page["slot_count"] for page in pages), len(times))
        self.assertEqual([page["page"] for page in pages], list(range(1, len(pages) + 1)))
        expected_start = 0
        for page in pages:
            self.assertEqual(page["start_index"], expected_start)
            expected_start += page["slot_count"]
        self.assertEqual(
            [value for page in pages for value in page["totaal_tarieven"]],
            prices,
        )

    def test_graph_index_publishes_topic_entities_and_legacy_arrays_when_small(self):
        planner = self.make_planner()
        planner.GRAPH_DATA_ENTITY = "sensor.test_graph"
        planner.GRAPH_ATTRIBUTE_MAX_BYTES = 12000
        planner.matrix_kwartieren = [{
            "tijd": datetime.datetime(2026, 10, 6, 12, 0, tzinfo=tz.UTC),
            "epex_prijs": 0.2,
            "net_prijs": 0.46,
            "laadbare_zon_kwh": 0.5,
            "verbruik_kw": 0.11,
            "prijs_bron": "epex",
        }]
        planner.plan = {}
        planner._tarief_meta = {}
        planner._forecast_status = None
        planner._plan_status = "concept"
        recorded = {}
        planner.set_state = lambda entity, state, attributes: recorded.setdefault(entity, attributes)

        planner._update_graph_data()

        index = recorded["sensor.test_graph"]
        self.assertEqual(set(index["data_entities"]), {"tijden", "prijzen", "energie", "planning"})
        self.assertTrue(index["legacy_arrays_available"])
        self.assertEqual(index["tijden"], ["2026-10-06 12:00"])
        self.assertEqual(index["data_entities"]["tijden"], ["sensor.test_graph_tijden_1"])


if __name__ == "__main__":
    unittest.main()
