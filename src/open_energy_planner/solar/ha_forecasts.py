"""Validate and retain provider-neutral HA production forecasts with honest knowledge times."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import (
    MAX_SOLAR_ENERGY_KWH,
    MIN_POSITIVE_SOLAR_SETTING,
    SolarImport,
    identity,
    number,
    version,
)
from open_energy_planner.solar.wire import document, parse_import

MAX_HA_FORECAST_HOURS = 8 * 24
MAX_HA_FORECAST_STEPS = MAX_HA_FORECAST_HOURS * 8
HA_REFRESH_MAX_AGE = timedelta(hours=2)
HA_RECEIPT_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class ProductionInterval:
    """Original normalized provider interval with aware start, elapsed hours and estimated AC kWh."""

    start: datetime
    duration_h: float
    kwh: float

    def __post_init__(self):
        """Preserve partial sunrise/sunset and native quarter intervals without claiming finer measurements."""
        object.__setattr__(self, "start", aware(self.start))
        number(self.duration_h, MIN_POSITIVE_SOLAR_SETTING, 1)
        number(self.kwh, 0, MAX_SOLAR_ENERGY_KWH)


@dataclass(frozen=True)
class HAProductionForecast:
    """Metered AC production, distinct from available panel DC and provider issuance."""

    configuration: SolarImport
    steps: tuple[ProductionInterval, ...]
    observed_at: datetime
    provider_refreshed_at: datetime | None
    configuration_revision: str
    provider_issued_at: datetime | None = None

    def __post_init__(self):
        """Require a bounded opaque configuration revision independent of changing forecast values."""
        identity(self.configuration_revision)

    @property
    def version(self):
        """Identify unchanged values independently of repeated reads of HA's cached response."""
        body = document(self)
        body.pop("observed_at")
        body.pop("provider_refreshed_at")
        return version(body)

    def fresh(self, now):
        """Require an observed real HA refresh. Merely discovering a cache cannot establish freshness."""
        return self.provider_refreshed_at is not None and (
            self.provider_refreshed_at <= now < self.provider_refreshed_at + HA_REFRESH_MAX_AGE
        )


def parse_ha_predictions(body, suggestions, now):
    """Require current exact Energy associations and bounded, ordered, nonoverlapping native intervals."""
    if not isinstance(body, dict) or set(body) != {"observed_at", "groups"}:
        raise ValueError("Invalid HA solar forecasts")
    observed = aware(datetime.fromisoformat(body["observed_at"]))
    if not now - HA_RECEIPT_TOLERANCE <= observed <= now:
        raise ValueError("Invalid HA solar observation time")
    groups = body["groups"]
    if not isinstance(groups, list):
        raise TypeError("Invalid HA solar groups")
    expected = {row["entity_id"]: row for row in suggestions if row["issue"] is None}
    result = {}
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"entity_id", "providers"}:
            raise ValueError("Invalid HA solar group")
        entity = group["entity_id"]
        if entity in result or entity not in expected:
            raise ValueError("HA solar association changed")
        providers = group["providers"]
        if not isinstance(providers, list) or len(providers) != len(expected[entity]["providers"]):
            raise ValueError("Incomplete HA solar group")
        forecasts = []
        for provider, metadata in zip(providers, expected[entity]["providers"], strict=True):
            if not isinstance(provider, dict) or set(provider) != {
                "adapter",
                "entry_id",
                "configuration",
                "configuration_revision",
                "steps",
                "provider_issued_at",
                "provider_refreshed_at",
            }:
                raise ValueError("Invalid HA solar provider")
            if provider["configuration"] != metadata or (
                provider["adapter"],
                provider["entry_id"],
            ) != (metadata["adapter"], metadata["entry_id"]):
                raise ValueError("HA solar provider changed")
            rows = provider["steps"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_HA_FORECAST_STEPS:
                raise ValueError("Invalid HA solar steps")
            steps = tuple(
                ProductionInterval(**{**row, "start": aware(datetime.fromisoformat(row["start"]))})
                for row in rows
            )
            previous_end = None
            for step in steps:
                if (
                    not observed - timedelta(days=1) <= step.start
                    or step.start + timedelta(hours=step.duration_h)
                    > observed + timedelta(hours=MAX_HA_FORECAST_HOURS)
                    or previous_end is not None
                    and step.start < previous_end
                ):
                    raise ValueError("Invalid HA solar coverage")
                previous_end = step.start + timedelta(hours=step.duration_h)
            refreshed = provider["provider_refreshed_at"]
            refreshed = aware(datetime.fromisoformat(refreshed)) if refreshed else None
            issued = provider["provider_issued_at"]
            issued = aware(datetime.fromisoformat(issued)) if issued else None
            if any(value is not None and value > observed for value in (refreshed, issued)):
                raise ValueError("Invalid HA solar knowledge time")
            forecasts.append(
                HAProductionForecast(
                    parse_import(metadata),
                    steps,
                    observed,
                    refreshed,
                    provider["configuration_revision"],
                    issued,
                )
            )
        result[entity] = tuple(forecasts)
    return result
