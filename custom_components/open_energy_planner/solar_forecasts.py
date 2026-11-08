"""Forward cached HA forecasts without provider requests or fabricated issue times."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import pairwise

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .solar_adapters import CONFIGURATION_ADAPTERS, _quantity

MAX_FORECAST_DAYS = 8
MAX_FORECAST_HOURS = MAX_FORECAST_DAYS * 24
MAX_FORECAST_STEPS = MAX_FORECAST_HOURS * 8
MAX_FORECAST_ENERGY_WH = 10000000
SECONDS_PER_HOUR = 3600
WATT_HOURS_PER_KWH = 1000
MAX_NATIVE_INTERVAL = timedelta(hours=1)


def _night_steps(start, end):
    """Split explicit outside-daylight zero production into bounded elapsed-time intervals."""
    steps = []
    cursor = dt_util.as_utc(start)
    end = dt_util.as_utc(end)
    while cursor < end:
        right = min(cursor + MAX_NATIVE_INTERVAL, end)
        steps.append(
            {
                "start": cursor.isoformat(),
                "duration_h": (right - cursor).total_seconds() / SECONDS_PER_HOUR,
                "kwh": 0,
            }
        )
        cursor = right
    return steps


async def forecast_solar_prediction(hass, entry):
    """Normalize cached native end-labelled Wh without the Core service's time-label ambiguity.

    The library retains raw period energy at its end timestamp. Its official
    cumulative fixture proves 465 minus 89 equals 376 Wh ending at 08:00.
    Read the coordinator's original library data within this provider adapter.
    A later Core service fix cannot silently change these interval semantics.
    """
    estimate = entry.runtime_data.data
    values = {
        instant.isoformat(): energy
        for instant, energy in estimate.wh_period.items()
        if instant in estimate.watts
    }
    if not isinstance(values, dict) or not 1 <= len(values) <= MAX_FORECAST_STEPS:
        raise ValueError("Invalid HA solar forecast")
    zone = await dt_util.async_get_time_zone(estimate.timezone)
    if zone is None:
        raise ValueError("Solar provider timezone unavailable")
    days = {}
    periods = []
    for timestamp, value in values.items():
        instant = dt_util.parse_datetime(timestamp)
        if instant is None or instant.tzinfo is None:
            raise ValueError("Invalid solar forecast time")
        instant = dt_util.as_utc(instant)
        energy = _quantity(value, 0, MAX_FORECAST_ENERGY_WH)
        periods.append((instant, energy))
        days.setdefault(instant.astimezone(zone).date(), []).append(
            (instant, energy, estimate.watts.get(instant))
        )
    if len(days) > MAX_FORECAST_DAYS:
        raise ValueError("Solar forecast exceeds bounded day count")
    periods.sort()
    steps = []
    for (before, _), (after, energy) in pairwise(periods):
        duration_h = (after - before).total_seconds() / SECONDS_PER_HOUR
        if not timedelta() < after - before <= MAX_NATIVE_INTERVAL:
            continue
        if energy == 0 and before.astimezone(zone).date() != after.astimezone(zone).date():
            continue
        steps.append(
            {
                "start": before.isoformat(),
                "duration_h": duration_h,
                "kwh": energy / WATT_HOURS_PER_KWH,
            }
        )
    # Zero endpoints and the complete provider daily total establish night coverage.
    # A partial cache ending at zero watts must not hide later daylight.
    for day, points in days.items():
        points.sort()
        # Date-only totals are provider-local calendar dates, not host-local instants.
        day_total = next(
            (
                total
                for timestamp, total in estimate.wh_days.items()
                if (
                    timestamp.astimezone(zone) if timestamp.tzinfo is not None else timestamp
                ).date()
                == day
            ),
            None,
        )
        if (
            len(points) < 3
            or points[0][1:] != (0, 0)
            or points[-1][2] != 0
            or day_total is None
            or sum(energy for _, energy, _ in points) != day_total
        ):
            continue
        midnight = datetime.combine(day, time(), zone)
        finish = datetime.combine(day + timedelta(days=1), time(), zone)
        steps.extend(_night_steps(midnight, points[0][0]))
        steps.extend(_night_steps(points[-1][0], finish))
    if not 1 <= len(steps) <= MAX_FORECAST_STEPS:
        raise ValueError("Solar forecast has no bounded usable intervals")
    return sorted(steps, key=lambda row: row["start"])


FORECAST_ADAPTERS = {"forecast_solar": forecast_solar_prediction}


def _configuration_revision(entry):
    """Identify HA entry/subentry changes without reading or hashing provider credentials."""
    return hashlib.sha256(f"{entry.entry_id}:{entry.modified_at.isoformat()}".encode()).hexdigest()


@dataclass
class _ProviderWatch:
    """Own one coordinator generation, credential-free configuration identity and refresh listener."""

    coordinator: object
    configuration_revision: str
    cancel: Callable[[], None] | None = None
    refreshed_at: datetime | None = None


class SolarForecastBridge:
    """Track real coordinator refreshes separately from OEP observing an existing HA cache."""

    def __init__(self, hass):
        """Own entry-local listeners. Provider issuance is unknown and never inferred from polling."""
        self.hass = hass
        self.watches = {}
        self.generation = 0

    def close(self):
        """Release listeners on unload and discard coordinator-specific freshness."""
        for watch in self.watches.values():
            if watch.cancel:
                watch.cancel()
        self.watches.clear()

    def watch(self, entry):
        """Only an observed successful provider refresh establishes provider freshness."""
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None or not hasattr(coordinator, "async_add_listener"):
            raise ValueError("Solar provider unavailable")
        identity = entry.entry_id
        revision = _configuration_revision(entry)
        previous = self.watches.get(identity)
        if previous and previous.coordinator is coordinator:
            if previous.configuration_revision == revision:
                return previous
            if previous.cancel:
                previous.cancel()
                previous.cancel = None
            previous.refreshed_at = None
            raise ValueError("Solar provider configuration awaits coordinator reload")
        if previous and previous.cancel:
            previous.cancel()
        watch = _ProviderWatch(coordinator, revision)
        self.watches[identity] = watch

        def updated():
            """Retain a successful HA refresh time, never relabel it as provider issuance."""
            self.generation += 1
            if coordinator.last_update_success and _configuration_revision(entry) == revision:
                watch.refreshed_at = dt_util.utcnow()

        watch.cancel = coordinator.async_add_listener(updated)
        return watch

    async def capture(self, suggestions, now):
        """Return complete selected groups with original adapter identities and missing-data gaps."""
        groups = []
        selected = {
            provider["entry_id"]
            for suggestion in suggestions
            if suggestion["issue"] is None
            for provider in suggestion["providers"]
        }
        for suggestion in suggestions:
            if suggestion["issue"] is not None:
                continue
            providers = []
            for provider in suggestion["providers"]:
                entry = self.hass.config_entries.async_get_entry(provider["entry_id"])
                adapter = FORECAST_ADAPTERS.get(entry.domain) if entry else None
                if adapter is None:
                    break
                try:
                    watch = self.watch(entry)
                    if not entry.runtime_data.last_update_success:
                        break
                    configuration_adapter = CONFIGURATION_ADAPTERS.get(entry.domain)
                    if configuration_adapter is None or configuration_adapter(entry) != provider:
                        break
                    original = entry.runtime_data.data
                    steps = await adapter(self.hass, entry)
                    if (
                        entry.runtime_data.data is not original
                        or configuration_adapter(entry) != provider
                        or _configuration_revision(entry) != watch.configuration_revision
                    ):
                        break
                except (HomeAssistantError, ValueError, TypeError, KeyError, AttributeError):
                    # One unavailable provider must not prevent independent meters from transferring.
                    break
                if any(
                    not now - timedelta(days=1)
                    <= dt_util.parse_datetime(row["start"])
                    <= now + timedelta(hours=MAX_FORECAST_HOURS)
                    for row in steps
                ):
                    break
                refreshed = watch.refreshed_at
                providers.append(
                    {
                        "adapter": provider["adapter"],
                        "entry_id": entry.entry_id,
                        "configuration": provider,
                        "configuration_revision": watch.configuration_revision,
                        "steps": steps,
                        "provider_issued_at": None,
                        "provider_refreshed_at": refreshed.isoformat() if refreshed else None,
                    }
                )
            else:
                groups.append({"entity_id": suggestion["entity_id"], "providers": providers})
        for identity in set(self.watches) - selected:
            watch = self.watches.pop(identity)
            if watch.cancel:
                watch.cancel()
        return {"observed_at": dt_util.utcnow().isoformat(), "groups": groups}
