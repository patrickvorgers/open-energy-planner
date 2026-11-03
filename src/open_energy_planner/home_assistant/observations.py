"""Typed measurement inputs and snapshot assembly, independent of HA and HTTP."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.installation.model import InstallationProblem, slice_interval
from open_energy_planner.planning import InputSnapshot

MAX_OBSERVATION_AGE_SECONDS = 300


@dataclass(frozen=True)
class SourceSpec:
    """Describe one app-owned sensor role and its measurement interpretation.

    Values arrive normalized as W, kWh or percent according to quantity. A power
    multiplier of -1 reverses the selected sensor's sign. Grid import and battery
    discharge use the positive convention. maximum_age_s bounds integration capture age.
    Original HA report and device sample times remain provenance only.
    Required controls whether an unusable source blocks planning readiness.
    """

    key: str
    entity_id: str
    quantity: str
    required: bool
    multiplier: int
    maximum_age_s: float
    timestamp_attribute: str | None


@dataclass(frozen=True)
class SourceConfiguration:
    """Freeze a source revision and its selected sensor bindings."""

    revision: str
    bindings: tuple[SourceSpec, ...]


@dataclass(frozen=True)
class Observation:
    """Record a normalized value with entity identity and distinct sample/report timestamps."""

    key: str
    entity_id: str
    value: float
    unit: str
    observed_at: datetime
    reported_at: datetime
    timestamp_kind: str


@dataclass(frozen=True)
class ObservationIssue:
    """Record an unavailable/unusable source without replacing its measurement with zero."""

    key: str
    entity_id: str
    reason: str


@dataclass(frozen=True)
class ObservationReport:
    """Freeze one complete capture, its source revision and explicit readings/issues."""

    version: str
    config_revision: str
    captured_at: datetime
    readings: tuple[Observation, ...]
    issues: tuple[ObservationIssue, ...]
    location: SiteLocation | None = None


@dataclass(frozen=True)
class PlanningBasis:
    """Independent versioned forecast/price data and configured physical limits.

    Current measured power is never substituted for future energy or prices.
    A provider must supply a new basis once coverage or source validity expires.
    """

    config_revision: str
    forecast_version: str
    price_version: str
    valid_until: datetime
    problem: InstallationProblem
    contract_version: str | None = None
    tariff_evidence: tuple = ()
    bonus_evidence: tuple = ()
    demand_evidence: tuple = ()
    solar_evidence: tuple = ()

    def __post_init__(self):
        """Require named input versions, aware validity and a validated physical problem."""
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.config_revision,
                self.forecast_version,
                self.price_version,
            )
        ):
            raise ValueError("Explicit configuration, forecast and price versions are required")
        if not isinstance(self.valid_until, datetime) or self.valid_until.utcoffset() is None:
            raise ValueError("Future-input validity requires a timezone")
        if not isinstance(self.problem, InstallationProblem):
            raise TypeError("Future-input basis requires a validated problem")


class BasisSource(Protocol):
    """Provide a nonblocking view of independently versioned future inputs."""

    def current(
        self,
        configuration: SourceConfiguration,
        now: datetime,
        *,
        report: ObservationReport | None = None,
    ) -> PlanningBasis | None:
        """Return cached forecast/prices for the configuration or None when unavailable."""
        ...


class InputsUnavailable(ValueError):
    """A safe, visible readiness reason rather than fabricated replacement data."""


def capture_deadline(spec: SourceSpec, report: ObservationReport):
    """Expire the current integration capture without assigning a heartbeat to sensor timestamps."""
    return min(
        report.captured_at + timedelta(seconds=MAX_OBSERVATION_AGE_SECONDS),
        report.captured_at + timedelta(seconds=spec.maximum_age_s),
    )


class SnapshotAssembler:
    """Combine validated current observations with independent future forecasts and prices."""

    def expiry(self, configuration, report, now):
        """Return the earliest required-source/report deadline or raise InputsUnavailable."""
        if report.config_revision != configuration.revision:
            raise InputsUnavailable("configuration_changed")
        if not 0 <= (now - report.captured_at).total_seconds() < MAX_OBSERVATION_AGE_SECONDS:
            raise InputsUnavailable("report_expired")
        readings = {reading.key: reading for reading in report.readings}
        issues = {issue.key for issue in report.issues}
        deadlines = [report.captured_at + timedelta(seconds=MAX_OBSERVATION_AGE_SECONDS)]
        for spec in configuration.bindings:
            if not spec.required:
                continue
            reading = readings.get(spec.key)
            if spec.key in issues or reading is None:
                raise InputsUnavailable("required_measurement_unavailable")
            deadline = capture_deadline(spec, report)
            if now >= deadline:
                raise InputsUnavailable("required_measurement_expired")
            deadlines.append(deadline)
        return min(deadlines)

    def build(self, configuration, report, basis, now):
        """Create immutable planning input with measured SoC and independently versioned forecasts.

        Clip a partially elapsed forecast interval using its average energy rate. Do
        not extrapolate present power into future load/PV. Bound validity by both source
        ages and future-input validity, retaining the original observation evidence.
        """
        until = self.expiry(configuration, report, now)
        if basis is None:
            raise InputsUnavailable("forecast_or_prices_unavailable")
        if (
            basis.config_revision != configuration.revision
            or not basis.forecast_version
            or not basis.price_version
            or basis.valid_until <= now
        ):
            raise InputsUnavailable("forecast_or_prices_invalid")
        problem = basis.problem
        intervals = tuple(
            step
            for step in problem.intervals
            if step.start + timedelta(hours=step.duration_h) > now
        )
        if not intervals or intervals[0].start > now:
            raise InputsUnavailable("forecast_or_prices_have_no_current_coverage")
        first = intervals[0]
        if first.start < now:
            intervals = (
                slice_interval(first, now, first.start + timedelta(hours=first.duration_h)),
                *intervals[1:],
            )
        until = min(until, basis.valid_until)
        return InputSnapshot(
            snapshot_id=str(uuid4()),
            config_revision=configuration.revision,
            measured_at=report.captured_at,
            measurement_version=report.version,
            forecast_version=basis.forecast_version,
            price_version=basis.price_version,
            problem=replace(problem, intervals=intervals),
            source="home_assistant",
            maximum_input_age_s=(until - report.captured_at).total_seconds(),
            measurement_evidence=report.readings,
            source_configuration=configuration.bindings,
            measurement_issues=report.issues,
            contract_version=basis.contract_version,
            tariff_evidence=basis.tariff_evidence,
            bonus_evidence=basis.bonus_evidence,
            demand_evidence=basis.demand_evidence,
            solar_evidence=basis.solar_evidence,
            site_location=report.location,
            future_valid_until=basis.valid_until,
        )
