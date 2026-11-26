"""Rebuild a safe planning input with charging treated as uncontrolled load."""

from dataclasses import replace
from datetime import timedelta
from math import fsum

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.demand.calculation.composition import integrals

EV_SERVICE_UNPROVEN = "ev_service_unproven"
EV_CONTROL_DEFERRED = "ev_control_deferred"
EV_FALLBACK_BUDGET_SECONDS = 20


def uncontrolled_snapshot(snapshot):
    """Restore charging forecasts and reserve headroom for their zero-to-maximum power range."""
    problem = snapshot.problem
    if not problem.ev_work or not snapshot.ev_power_bounds:
        return None
    bounds = dict(snapshot.ev_power_bounds)
    chargers = [
        component for component in snapshot.load_components if component.entity_id in bounds
    ]
    if {component.entity_id for component in chargers} != bounds.keys():
        return None
    windows = tuple(
        (interval.start, interval.start + timedelta(hours=interval.duration_h))
        for interval in problem.intervals
    )
    expected, retained = [], []
    for component in chargers:
        try:
            forecast = integrals(component.forecast_steps, windows)
            retained.append(integrals(component.steps, windows))
        except ValueError:
            return None
        if any(
            energy * WATTS_PER_KW / interval.duration_h > bounds[component.entity_id]
            for energy, interval in zip(forecast, problem.intervals, strict=True)
        ):
            return None
        expected.append(forecast)
    expected_w = [
        fsum(energy) * WATTS_PER_KW / interval.duration_h
        for interval, energy in zip(problem.intervals, zip(*expected, strict=True), strict=True)
    ]
    # One conservative pair of limits covers every interval, including zero charging.
    import_limit = problem.grid_import_limit_w - (sum(bounds.values()) - min(expected_w))
    export_limit = problem.grid_export_limit_w - max(expected_w)
    if min(import_limit, export_limit) < 0:
        return None
    intervals = tuple(
        replace(interval, load_kwh=interval.load_kwh + fsum(forecast) - fsum(existing))
        for interval, forecast, existing in zip(
            problem.intervals, zip(*expected, strict=True), zip(*retained, strict=True), strict=True
        )
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
