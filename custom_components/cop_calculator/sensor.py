from __future__ import annotations

from datetime import datetime
from collections import defaultdict
from typing import Optional, Dict, Tuple, List

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, DEFAULT_LANGUAGE, DEFAULT_PUMP_POWER


# ============================================================
# Power integration (W -> kWh) with period aggregation
# ============================================================

class PowerIntegrator:
    """
    Integrates power measurements (W) into energy (kWh).
    Keeps everything in memory to avoid recorder load.
    """

    def __init__(self) -> None:
        self._samples: List[Tuple[datetime, float]] = []

    def add_sample(self, power_w: Optional[float], ts: Optional[datetime] = None) -> None:
        if power_w is None:
            return

        # Safety check: avoid kW/W mismatch
        if power_w < 0.01:
            power_w *= 1000

        if ts is None:
            ts = datetime.now()

        self._samples.append((ts, power_w))

    def energy_kwh(self) -> float:
        if len(self._samples) < 2:
            return 0.0

        total_wh = 0.0
        for i in range(1, len(self._samples)):
            t1, p1 = self._samples[i - 1]
            t2, p2 = self._samples[i]
            dt_h = (t2 - t1).total_seconds() / 3600
            total_wh += ((p1 + p2) / 2) * dt_h

        return total_wh / 1000

    def energy_by_month(self) -> Dict[Tuple[int, int], float]:
        grouped = defaultdict(list)
        for ts, p in self._samples:
            grouped[(ts.year, ts.month)].append((ts, p))

        result: Dict[Tuple[int, int], float] = {}
        for key, samples in grouped.items():
            if len(samples) < 2:
                result[key] = 0.0
                continue

            wh = 0.0
            for i in range(1, len(samples)):
                t1, p1 = samples[i - 1]
                t2, p2 = samples[i]
                dt_h = (t2 - t1).total_seconds() / 3600
                wh += ((p1 + p2) / 2) * dt_h

            result[key] = wh / 1000

        return result

    def energy_by_year(self) -> Dict[int, float]:
        grouped = defaultdict(list)
        for ts, p in self._samples:
            grouped[ts.year].append((ts, p))

        result: Dict[int, float] = {}
        for year, samples in grouped.items():
            if len(samples) < 2:
                result[year] = 0.0
                continue

            wh = 0.0
            for i in range(1, len(samples)):
                t1, p1 = samples[i - 1]
                t2, p2 = samples[i]
                dt_h = (t2 - t1).total_seconds() / 3600
                wh += ((p1 + p2) / 2) * dt_h

            result[year] = wh / 1000

        return result


# ============================================================
# DHW thermal energy calculation (tank based)
# ============================================================

class DHWThermalCalculator:
    """
    Calculates thermal energy for a DHW run based on tank temperature.
    Correctly handles intermediate temperature drops.
    """

    def __init__(self, volume_liters: float = 260.0) -> None:
        self.volume_l = volume_liters
        self.reset()

    def reset(self) -> None:
        self.start_temp: Optional[float] = None
        self.last_temp: Optional[float] = None
        self.min_temp: Optional[float] = None
        self.extra_drop: float = 0.0

    def start(self, temp_c: float) -> None:
        self.start_temp = temp_c
        self.last_temp = temp_c
        self.min_temp = temp_c
        self.extra_drop = 0.0

    def update(self, temp_c: float) -> None:
        if self.last_temp is None:
            self.last_temp = temp_c
            return

        delta = temp_c - self.last_temp
        if delta < 0:
            self.extra_drop += abs(delta)

        self.last_temp = temp_c
        self.min_temp = min(self.min_temp, temp_c) if self.min_temp is not None else temp_c

    def thermal_energy_kwh(self) -> Optional[float]:
        if self.start_temp is None or self.last_temp is None or self.min_temp is None:
            return None

        total_delta = (self.last_temp - self.min_temp) + self.extra_drop
        # 4.18 kJ/kgK, 1L ≈ 1kg
        return (self.volume_l * total_delta * 4.18) / 3600


# ============================================================
# Electrical power allocation (single source of truth)
# ============================================================

class ElectricalPowerAllocator:
    """
    Central logic for correct electrical power attribution.
    Prevents double counting and pump leakage into DHW.
    """

    def __init__(self, pump_power_w: float) -> None:
        self.pump_power = pump_power_w

    def heating_or_cooling_power(
        self,
        outside_power: float,
    ) -> float:
        """
        Space heating / cooling:
        - Outside unit
        - Circulation pump ONLY
        - Never electric heater
        """
        return outside_power + self.pump_power

    def dhw_heatpump_power(
        self,
        outside_power: float,
        inside_power: float,
    ) -> float:
        """
        DHW via heat pump:
        - Outside unit
        - Inside unit
        - Minus circulation pump
        """
        return max(outside_power + inside_power - self.pump_power, 0.0)

    def dhw_heater_power(
        self,
        inside_power: float,
    ) -> float:
        """
        DHW via electric heater only:
        - Inside unit
        - Minus circulation pump
        """
        return max(inside_power - self.pump_power, 0.0)

# ============================================================
# COP calculation helpers
# ============================================================

class COPCalculator:
    """
    Generic COP calculator.
    Protects against division by zero and negative power.
    """

    @staticmethod
    def cop(thermal_kwh: float, electrical_kwh: float) -> Optional[float]:
        if thermal_kwh is None or electrical_kwh is None:
            return None
        if electrical_kwh <= 0:
            return None
        return thermal_kwh / electrical_kwh


# ============================================================
# Heating / Cooling runtime COP
# ============================================================

class SpaceConditioningRuntime:
    """
    Handles heating OR cooling (never both simultaneously).
    """

    def __init__(self) -> None:
        self.thermal_integrator = PowerIntegrator()
        self.electrical_integrator = PowerIntegrator()

    def add_sample(
        self,
        thermal_power_w: Optional[float],
        electrical_power_w: Optional[float],
        ts: Optional[datetime] = None,
    ) -> None:
        self.thermal_integrator.add_sample(thermal_power_w, ts)
        self.electrical_integrator.add_sample(electrical_power_w, ts)

    def cop(self) -> Optional[float]:
        return COPCalculator.cop(
            self.thermal_integrator.energy_kwh(),
            self.electrical_integrator.energy_kwh(),
        )

    def monthly_cop(self) -> Dict[Tuple[int, int], Optional[float]]:
        th = self.thermal_integrator.energy_by_month()
        el = self.electrical_integrator.energy_by_month()

        result: Dict[Tuple[int, int], Optional[float]] = {}
        for key in th:
            result[key] = COPCalculator.cop(th[key], el.get(key, 0.0))
        return result

    def yearly_cop(self) -> Dict[int, Optional[float]]:
        th = self.thermal_integrator.energy_by_year()
        el = self.electrical_integrator.energy_by_year()

        result: Dict[int, Optional[float]] = {}
        for year in th:
            result[year] = COPCalculator.cop(th[year], el.get(year, 0.0))
        return result


# ============================================================
# DHW runtime tracking (heat pump + electric heater)
# ============================================================

class DHWRuntime:
    """
    Tracks a single DHW run and aggregates lifetime/month/year.
    Correctly handles:
    - DHW via heat pump (operation_state_dhw_on)
    - DHW via electric heater (binary_sensor_dhw_heater_2)
    - parallel DHW electric + space heating
    """

    def __init__(self, tank_volume_l: float, pump_power_w: float) -> None:
        self.thermal_calc = DHWThermalCalculator(tank_volume_l)
        self.electrical_integrator = PowerIntegrator()

        self.allocator = ElectricalPowerAllocator(pump_power_w)

        self.active = False
        self.last_run_cop: Optional[float] = None

    # --------------------
    # Lifecycle
    # --------------------

    def start(self, tank_temp_c: float) -> None:
        self.active = True
        self.thermal_calc.start(tank_temp_c)
        self.electrical_integrator = PowerIntegrator()

    def stop(self) -> None:
        if not self.active:
            return

        thermal_kwh = self.thermal_calc.thermal_energy_kwh()
        electrical_kwh = self.electrical_integrator.energy_kwh()

        self.last_run_cop = COPCalculator.cop(thermal_kwh, electrical_kwh)
        self.active = False

    # --------------------
    # Updates
    # --------------------

    def update_tank_temp(self, temp_c: float) -> None:
        if self.active:
            self.thermal_calc.update(temp_c)

    def add_heatpump_power(
        self,
        outside_power_w: float,
        inside_power_w: float,
        ts: Optional[datetime] = None,
    ) -> None:
        """
        DHW via heat pump.
        Pompvermogen wordt expliciet afgetrokken.
        """
        if not self.active:
            return

        power = self.allocator.dhw_heatpump_power(
            outside_power_w,
            inside_power_w,
        )
        self.electrical_integrator.add_sample(power, ts)

    def add_heater_power(
        self,
        inside_power_w: float,
        ts: Optional[datetime] = None,
    ) -> None:
        """
        DHW via electric heater.
        Pompvermogen wordt expliciet afgetrokken.
        """
        if not self.active:
            return

        power = self.allocator.dhw_heater_power(inside_power_w)
        self.electrical_integrator.add_sample(power, ts)

    # --------------------
    # Aggregations
    # --------------------

    def lifetime_cop(self) -> Optional[float]:
        return COPCalculator.cop(
            self.thermal_calc.thermal_energy_kwh(),
            self.electrical_integrator.energy_kwh(),
        )

    def monthly_cop(self) -> Dict[Tuple[int, int], Optional[float]]:
        th = self.thermal_calc.thermal_energy_kwh()
        el = self.electrical_integrator.energy_by_month()

        result: Dict[Tuple[int, int], Optional[float]] = {}
        for key, el_kwh in el.items():
            result[key] = COPCalculator.cop(th, el_kwh)
        return result

    def yearly_cop(self) -> Dict[int, Optional[float]]:
        th = self.thermal_calc.thermal_energy_kwh()
        el = self.electrical_integrator.energy_by_year()

        result: Dict[int, Optional[float]] = {}
        for year, el_kwh in el.items():
            result[year] = COPCalculator.cop(th, el_kwh)
        return result

