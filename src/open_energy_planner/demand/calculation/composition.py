"""Preserve the measured household boundary while replacing acknowledged device demand."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from math import fsum, isclose, isfinite

from open_energy_planner.demand.calculation.model import quantity

LOAD_BALANCE_TOLERANCE_KWH = 0.0000001


@dataclass(frozen=True)
class LoadStep:
    """An AC kWh contribution with exact service-window boundaries, including partial quarters."""

    start: datetime
    duration_h: float
    kwh: float

    def __post_init__(self):
        """Keep arbitrary refinement boundaries finite, nonnegative and timezone aware."""
        if self.start.utcoffset() is None or not isfinite(self.duration_h) or self.duration_h <= 0:
            raise ValueError("Invalid household component interval")
        quantity(self.kwh)


@dataclass(frozen=True)
class LoadComponent:
    """One frozen AC contribution in kWh, before separately scheduled work is added.

    A null entity identifies the remaining household boundary. Forecast steps
    retain the original expectation. Steps retain the expectation after concrete
    work has replaced its share. Neither series authorizes device execution.
    """

    entity_id: str | None
    forecast_version: str
    forecast_steps: tuple[LoadStep, ...]
    steps: tuple[LoadStep, ...]
    unseparated_entities: tuple[str, ...] = ()


def integral(steps, start, end):
    """Integrate complete nonoverlapping typed forecast coverage without inventing missing energy."""
    cursor, amounts = start, []
    for step in steps:
        left = max(start, step.start)
        right = min(end, step.start + timedelta(hours=step.duration_h))
        if right <= left:
            continue
        if left != cursor:
            raise ValueError("Household component coverage missing")
        amounts.append(step.kwh * ((right - left) / timedelta(hours=step.duration_h)))
        cursor = right
    if cursor != end:
        raise ValueError("Household component coverage missing")
    return fsum(amounts)


def integrals(steps, windows):
    """Integrate ordered, nonoverlapping windows in one sweep through exact source coverage."""
    spans = tuple(
        (step.start, step.start + timedelta(hours=step.duration_h), step.kwh) for step in steps
    )
    if any(previous[1] > current[0] for previous, current in pairwise(spans)):
        raise ValueError("Household component coverage overlaps")
    source_index, previous_end, result = 0, None, []
    for start, end in windows:
        if end <= start or (previous_end is not None and start < previous_end):
            raise ValueError("Household integration windows overlap or are invalid")
        previous_end = end
        while source_index < len(spans) and spans[source_index][1] <= start:
            source_index += 1
        cursor, amounts, index = start, [], source_index
        while index < len(spans) and spans[index][0] < end:
            source_start, source_end, energy = spans[index]
            left, right = max(start, source_start), min(end, source_end)
            if left != cursor:
                raise ValueError("Household component coverage missing")
            amounts.append(energy * ((right - left) / (source_end - source_start)))
            cursor = right
            index += 1
        if cursor != end:
            raise ValueError("Household component coverage missing")
        result.append(fsum(amounts))
    return tuple(result)


def replace_cycle(steps, work, horizon_start, horizon_end):
    """Replace at most one cycle's expected energy inside its acknowledged service window.

    An unconditional forecast cannot identify a particular future cycle. Debit
    its expected energy proportionally inside the ready/deadline window, capped
    by the remaining concrete cycle energy. Preserve additional expected use
    and all expectation outside that window. A fixed cycle debits only its
    remaining execution window after replanning.
    """
    left = max(horizon_start, work.fixed_start or work.ready_at)
    right = min(
        horizon_end,
        work.fixed_start + timedelta(seconds=work.profile.duration_seconds)
        if work.fixed_start
        else work.deadline,
    )
    if left >= right:
        return steps
    expected = integral(steps, left, right)
    remaining = (
        work.profile.energy(work.fixed_start, left, right)
        if work.fixed_start
        else work.profile.energy_kwh
    )
    fraction = min(1, remaining / expected) if expected else 0
    return scale_window(steps, left, right, fraction)


def scale_window(steps, left, right, fraction):
    """Remove a declared fraction of expectation only inside an independently planned window."""
    result = []
    for step in steps:
        stop = step.start + timedelta(hours=step.duration_h)
        boundaries = sorted(
            {step.start, stop, max(step.start, min(stop, left)), max(step.start, min(stop, right))}
        )
        for start, end in pairwise(boundaries):
            amount = step.kwh * ((end - start) / timedelta(hours=step.duration_h))
            if left <= start < right:
                amount *= 1 - fraction
            result.append(LoadStep(start, (end - start) / timedelta(hours=1), amount))
    return tuple(result)


def validate_components(problem, components, *, controlled_entities=()):
    """Reject missing control ownership, duplicate meters, coverage gaps and load differences.

    Uncontrolled household-only problems may omit components. Controlled problems
    require explicit meter ownership and one source identity per EV/thermal work.
    Invalid or absent required evidence raises ValueError without changing inputs.
    """
    controlled_entities = tuple(controlled_entities)
    controlled = [work.entity_id for work in problem.appliances] + list(controlled_entities)
    if not components:
        if controlled or problem.ev_work or problem.thermal_work:
            raise ValueError("Household component ownership unavailable")
        return
    if len(controlled_entities) != len(problem.ev_work) + len(problem.thermal_work):
        raise ValueError("Household component ownership unavailable")
    entities = [component.entity_id for component in components]
    if len(set(entities)) != len(entities) or entities.count(None) != 1:
        raise ValueError("Household component identities overlap")
    if len(set(controlled)) != len(controlled) or not set(controlled) <= set(entities):
        raise ValueError("Controlled household components overlap")
    windows = tuple(
        (interval.start, interval.start + timedelta(hours=interval.duration_h))
        for interval in problem.intervals
    )
    remaining = [integrals(component.steps, windows) for component in components]
    forecasts = [integrals(component.forecast_steps, windows) for component in components]
    for interval_index, interval in enumerate(problem.intervals):
        amounts = [series[interval_index] for series in remaining]
        if not isclose(
            fsum(amounts), interval.load_kwh, rel_tol=0, abs_tol=LOAD_BALANCE_TOLERANCE_KWH
        ):
            raise ValueError("Household component balance differs")
        for series, amount in zip(forecasts, amounts, strict=True):
            original = series[interval_index]
            if amount < 0 or amount > original + LOAD_BALANCE_TOLERANCE_KWH:
                raise ValueError("Household component replacement differs")
