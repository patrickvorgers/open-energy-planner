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
    """Reject duplicate meters, missing coverage and load differences before plan publication."""
    if not components:
        return
    entities = [component.entity_id for component in components]
    if len(set(entities)) != len(entities) or entities.count(None) != 1:
        raise ValueError("Household component identities overlap")
    controlled = [work.entity_id for work in problem.appliances] + list(controlled_entities)
    if len(set(controlled)) != len(controlled) or not set(controlled) <= set(entities):
        raise ValueError("Controlled household components overlap")
    for interval in problem.intervals:
        end = interval.start + timedelta(hours=interval.duration_h)
        amounts = [integral(component.steps, interval.start, end) for component in components]
        if not isclose(
            fsum(amounts), interval.load_kwh, rel_tol=0, abs_tol=LOAD_BALANCE_TOLERANCE_KWH
        ):
            raise ValueError("Household component balance differs")
        for component, amount in zip(components, amounts, strict=True):
            original = integral(component.forecast_steps, interval.start, end)
            if amount < 0 or amount > original + LOAD_BALANCE_TOLERANCE_KWH:
                raise ValueError("Household component replacement differs")
