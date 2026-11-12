"""Conservative, bounded future-use estimates. No solver or provider dependencies."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import fsum

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.solar.calculation.model import identity, number

from ..planning_policy import TERMINAL_VALUATION_HOURS

MAX_VALUATION_VOLUME_KWH = 1000000000000.0
INVENTORY_ROUNDOFF_TOLERANCE_KWH = 1e-07
MIN_POSITIVE_VALUE_OR_VOLUME = 1e-12


@dataclass(frozen=True)
class TerminalValuation:
    """Freeze one average net internal-kWh value and its bounded opportunity volume.

    Attributes:
        device_id: Stable battery identity within the installation.
        reserve_kwh: Internal energy inaccessible to economic discharge.
        usable_kwh: Maximum internal energy with an estimated later use opportunity.
        rate: Net currency per internal kWh, after discharge losses and future wear.
        window_start: Aware start of the later-price window or reused source window.
        window_end: Exclusive aware end, at most 24 hours after window_start.
        price_basis: Whether prices are later forecasts or a trailing-window proxy.
        tariff_evidence: Immutable selected later tariffs. Proxy origins remain in
            the planning input's ordinary tariff evidence.
        method: Versioned identity of this bounded-average calculation.
    """

    device_id: str
    reserve_kwh: float
    usable_kwh: float
    rate: float
    window_start: datetime
    window_end: datetime
    price_basis: str
    tariff_evidence: tuple = ()
    method: str = "bounded-average-v1"

    def __post_init__(self):
        """Require finite nonnegative estimates and explicit immutable aware evidence bounds."""
        identity(self.device_id)
        number(self.reserve_kwh, 0, MAX_VALUATION_VOLUME_KWH)
        number(self.usable_kwh, 0, MAX_VALUATION_VOLUME_KWH)
        number(self.rate, 0, MAX_VALUATION_VOLUME_KWH)
        if (
            not isinstance(self.tariff_evidence, tuple)
            or not isinstance(self.window_start, datetime)
            or not isinstance(self.window_end, datetime)
            or self.window_start.utcoffset() is None
            or self.window_end.utcoffset() is None
            or not self.window_start
            < self.window_end
            <= self.window_start + timedelta(hours=TERMINAL_VALUATION_HOURS)
            or self.price_basis not in ("post_horizon_prices", "trailing_window_proxy")
            or self.method != "bounded-average-v1"
        ):
            raise ValueError("Invalid terminal valuation evidence")

    def value(self, stored_kwh):
        """Return a currency amount for usable remaining internal energy.

        Accept up to 1e-7 kWh of negative solver roundoff, matching the default
        independent physical validator, and clamp it to zero. Larger negative
        or non-finite values are invalid. Reserve and the useful-volume cap apply.
        """
        number(stored_kwh, -INVENTORY_ROUNDOFF_TOLERANCE_KWH, MAX_VALUATION_VOLUME_KWH)
        return min(max(0, stored_kwh - self.reserve_kwh), self.usable_kwh) * self.rate


def derive(problem, future_tariffs=()):
    """Estimate one discharge-only continuation, allocating shared capacity once.

    Prefer up to 24 contiguous hours of effective prices after the command horizon.
    Repeat the last available up-to-24-hour physical profile, splitting at tariff
    edges. Without later prices, replay that window's effective prices explicitly
    as a proxy. This is an inventory estimate, never extra executable plan steps.

    Args:
        problem: Validated InstallationProblem with effective prices, forecast
            kWh, independent batteries and shared converter/connection limits.
        future_tariffs: Ordered immutable effective tariff evidence beginning
            exactly at the command horizon end, without gaps or overlaps.

    Returns:
        One immutable TerminalValuation per battery, or an empty tuple without
        batteries. Rates are currency/internal-kWh. Reserves and caps are kWh.

    Raises:
        ValueError: Later coverage is nonpositive, noncontiguous from the horizon
            end, extends beyond 24 hours, or produces invalid valuation evidence.
    """
    from .model import slice_interval

    end = problem.intervals[-1].start + timedelta(hours=problem.intervals[-1].duration_h)
    begin = max(problem.intervals[0].start, end - timedelta(hours=TERMINAL_VALUATION_HOURS))
    template = []
    for step in problem.intervals:
        left = max(begin, step.start)
        right = step.start + timedelta(hours=step.duration_h)
        if left < right:
            template.append(slice_interval(step, left, right))
    samples = []
    if future_tariffs:
        cursor = end
        for tariff in future_tariffs:
            if (
                tariff.start != cursor
                or tariff.end <= cursor
                or tariff.end > end + timedelta(hours=TERMINAL_VALUATION_HOURS)
            ):
                raise ValueError("Terminal prices require contiguous bounded future coverage")
            while cursor < tariff.end:
                offset = (cursor - end) % (end - begin)
                source = next(
                    source_item
                    for source_item in template
                    if source_item.start
                    <= begin + offset
                    < source_item.start + timedelta(hours=source_item.duration_h)
                )
                right = min(
                    tariff.end,
                    cursor + (source.start + timedelta(hours=source.duration_h) - begin - offset),
                )
                sampled = slice_interval(source, source.start, source.start + (right - cursor))
                samples.append(
                    replace(sampled, buy_price=tariff.buy_price, sell_price=tariff.sell_price)
                )
                cursor = right
        window_start, window_end, basis = end, cursor, "post_horizon_prices"
    else:
        samples = template
        window_start, window_end, basis = begin, end, "trailing_window_proxy"
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    opportunities = {battery_item.device_id: [] for battery_item in problem.batteries}
    for step in samples:
        pv_dc = {
            key: fsum(
                dict(step.pv_sources)[pv_source.source_id]
                for pv_source in problem.pv_sources
                if pv_source.inverter_id == key
            )
            for key in inverters
        }
        pv_ac = {
            key: min(pv_dc[key] * inv.efficiency, inv.ac_limit_w * step.duration_h / WATTS_PER_KW)
            for key, inv in inverters.items()
        }
        limits = {
            battery_item.device_id: battery_item.battery.discharge_limit_w
            * step.duration_h
            / WATTS_PER_KW
            * battery_item.dc_efficiency
            * inverters[battery_item.inverter_id].efficiency
            for battery_item in problem.batteries
        }
        for key, inv in inverters.items():
            local = [
                battery_item
                for battery_item in problem.batteries
                if battery_item.inverter_id == key
            ]
            total = fsum(limits[local_battery_entry.device_id] for local_battery_entry in local)
            headroom = max(0, inv.ac_limit_w * step.duration_h / WATTS_PER_KW - pv_ac[key])
            for local_battery in local:
                limits[local_battery.device_id] *= min(1, headroom / total) if total else 0
        residual = step.load_kwh - fsum(pv_ac.values())
        budget = (
            min(residual, problem.grid_import_limit_w * step.duration_h / WATTS_PER_KW)
            if residual > 0
            else max(0, problem.grid_export_limit_w * step.duration_h / WATTS_PER_KW + residual)
        )
        price = step.buy_price if residual > 0 else step.sell_price
        total = fsum(limits.values())
        share = min(1, budget / total) if total else 0
        for unit in problem.batteries:
            battery = unit.battery
            efficiency = (
                battery.discharge_efficiency
                * unit.dc_efficiency
                * inverters[unit.inverter_id].efficiency
            )
            rate = price * efficiency - battery.wear_cost_per_kwh
            amount = limits[unit.device_id] * share / efficiency
            if rate > MIN_POSITIVE_VALUE_OR_VOLUME and amount > MIN_POSITIVE_VALUE_OR_VOLUME:
                opportunities[unit.device_id].append((amount, rate))
    valuations = []
    for unit in problem.batteries:
        amounts = opportunities[unit.device_id]
        volume = fsum(amount for amount, _ in amounts)
        average = fsum(amount * rate for amount, rate in amounts) / volume if volume else 0
        valuations.append(
            TerminalValuation(
                unit.device_id,
                unit.battery.minimum_kwh,
                min(volume, unit.battery.capacity_kwh - unit.battery.minimum_kwh),
                average,
                window_start,
                window_end,
                basis,
                tuple(future_tariffs),
            )
        )
    return tuple(valuations)
