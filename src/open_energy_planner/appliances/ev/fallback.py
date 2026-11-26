"""Rebuild a safe planning input with charging treated as uncontrolled load."""

from dataclasses import replace
from datetime import timedelta
from math import fsum

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.demand.calculation.composition import integrals

EV_SERVICE_UNPROVEN = "ev_service_unproven"
EV_FALLBACK_REASONS = (EV_SERVICE_UNPROVEN, "ev_service_time_limit")
EV_CONTROL_DEFERRED = "ev_control_deferred"
EV_FALLBACK_BUDGET_SECONDS = 20


def _charging_series(chargers, bounds, intervals, windows):
    """Return original charge and the portion still present in the prepared household load."""
    expected, retained = [], []
    for component in chargers:
        forecast = integrals(component.forecast_steps, windows)
        if any(
            energy * WATTS_PER_KW / interval.duration_h > bounds[component.entity_id]
            for energy, interval in zip(forecast, intervals, strict=True)
        ):
            raise ValueError("Charging forecast exceeds the configured charge-point power")
        expected.append(forecast)
        retained.append(integrals(component.steps, windows))
    return tuple(map(fsum, zip(*expected, strict=True))), tuple(
        map(fsum, zip(*retained, strict=True))
    )


def uncontrolled_snapshot(snapshot):
    """Restore charging forecasts and reserve headroom for their zero-to-maximum power range."""
    problem = snapshot.problem
    if not problem.ev_work or not snapshot.ev_power_bounds:
        return None
    bounds = dict(snapshot.ev_power_bounds)
    chargers = [
        component for component in snapshot.load_components if component.entity_id in bounds
    ]
    if (
        len(chargers) != len(bounds)
        or {component.entity_id for component in chargers} != bounds.keys()
    ):
        return None
    windows = tuple(
        (interval.start, interval.start + timedelta(hours=interval.duration_h))
        for interval in problem.intervals
    )
    try:
        expected, retained = _charging_series(chargers, bounds, problem.intervals, windows)
    except ValueError:
        return None
    expected_w = [
        energy * WATTS_PER_KW / interval.duration_h
        for interval, energy in zip(problem.intervals, expected, strict=True)
    ]
    # One conservative pair of limits covers every interval, including zero charging.
    import_limit = problem.grid_import_limit_w - (sum(bounds.values()) - min(expected_w))
    export_limit = problem.grid_export_limit_w - max(expected_w)
    if min(import_limit, export_limit) < 0:
        return None
    intervals = tuple(
        replace(interval, load_kwh=interval.load_kwh + forecast - existing)
        for interval, forecast, existing in zip(problem.intervals, expected, retained, strict=True)
    )
    return replace(
        snapshot,
        problem=replace(
            problem,
            intervals=intervals,
            grid_import_limit_w=import_limit,
            grid_export_limit_w=export_limit,
            batteries=tuple(
                replace(battery, can_follow_grid=False) for battery in problem.batteries
            ),
            ev_work=(),
        ),
        ev_evidence=(),
        load_components=tuple(
            replace(component, steps=component.forecast_steps)
            if component.entity_id in bounds
            else component
            for component in snapshot.load_components
        ),
    )
