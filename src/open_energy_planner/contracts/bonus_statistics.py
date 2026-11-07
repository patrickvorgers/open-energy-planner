"""Validate annual export statistics and retain immutable references across refreshes."""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite

from open_energy_planner.core.units import BYTES_PER_KIB
from open_energy_planner.history.model import (
    MAX_CUMULATIVE_ENERGY_KWH,
    MAX_HISTORY_HOURS,
)
from open_energy_planner.prices.model import aware

# Sixteen annual channels with full-precision JSON numbers exceed 8 MiB.
MAX_STATISTICS_REPORT_BYTES = 16 * BYTES_PER_KIB * BYTES_PER_KIB
STATISTICS_MAX_AGE = timedelta(hours=3)
STATISTICS_HOUR = timedelta(hours=1)


def digest(value):
    """Bind references and historical checkpoints to exact normalized evidence."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def energy(value):
    """Accept finite signed statistics sums in kWh, excluding booleans and overflow."""
    if (
        type(value) not in (int, float)
        or abs(value) > MAX_CUMULATIVE_ENERGY_KWH
        or not isfinite(value)
    ):
        raise ValueError("Invalid statistics sum")
    return value


@dataclass(frozen=True)
class StatisticReference:
    """One HA Recorder series identity and its reference and latest sums in kWh."""

    statistic_id: str
    metadata_id: int
    source: str
    unit: str
    reference_sum: float
    latest_sum: float

    def __post_init__(self):
        """Validate Recorder identity and bounded signed sums normalized to kWh.

        Raw signed sums remain unchanged. exported_kwh floors a negative
        period difference without moving the reference.

        Raises:
            ValueError: Identity or sums are invalid.
        """
        if (
            not isinstance(self.statistic_id, str)
            or not self.statistic_id.startswith("sensor.")
            or type(self.metadata_id) is not int
            or self.metadata_id < 1
            or not isinstance(self.source, str)
            or not self.source
            or self.unit not in ("Wh", "kWh", "MWh")
        ):
            raise ValueError("Invalid statistics identity")
        energy(self.reference_sum)
        energy(self.latest_sum)

    @property
    def exported_kwh(self):
        """Keep raw evidence while treating a negative period difference as no export."""
        return max(0.0, self.latest_sum - self.reference_sum)


@dataclass(frozen=True)
class StatisticsBasis:
    """Persist reference, manual prefix, checkpoint and validation state with the counter.

    Timestamps are completed-hour endpoints, rather than HA row start times.
    A missing prefix remains unknown. A failed revalidation blocks use of the
    retained checkpoint, without deleting the evidence needed for recovery.
    """

    reference_at: datetime
    latest_at: datetime
    series: tuple[StatisticReference, ...]
    prefix_kwh: float | None
    confirmed_at: datetime | None
    history_digest: str
    physical_available: bool
    issue: str | None = None
    rejected_digest: str | None = None

    def __post_init__(self):
        """Validate the durable JSON contract independently of the reporting adapter."""
        for key in ("reference_at", "latest_at"):
            value = aware(getattr(self, key))
            if value.minute or value.second or value.microsecond:
                raise ValueError("Statistics endpoints must align to UTC hours")
            object.__setattr__(self, key, value)
        if self.latest_at < self.reference_at:
            raise ValueError("Statistics checkpoint predates reference")
        if (
            not isinstance(self.series, tuple)
            or not self.series
            or any(not isinstance(item, StatisticReference) for item in self.series)
            or len({item.statistic_id for item in self.series}) != len(self.series)
        ):
            raise ValueError("Invalid statistics members")
        if self.prefix_kwh is not None and energy(self.prefix_kwh) < 0:
            raise ValueError("Invalid missing-period export")
        if self.confirmed_at is not None:
            object.__setattr__(self, "confirmed_at", aware(self.confirmed_at))
            if self.confirmed_at < self.reference_at or self.prefix_kwh is None:
                raise ValueError("Invalid prefix confirmation")
        if (
            not isinstance(self.history_digest, str)
            or len(self.history_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.history_digest)
            or type(self.physical_available) is not bool
            or self.issue not in (None, "bonus_statistics_revalidation_required")
        ):
            raise ValueError("Invalid statistics validation evidence")
        if self.rejected_digest is not None and (
            not isinstance(self.rejected_digest, str)
            or len(self.rejected_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.rejected_digest)
        ):
            raise ValueError("Invalid rejected statistics evidence")

    @property
    def physical_settlement_ready(self):
        """Require validated usage and an exact physical endpoint without a live freshness deadline."""
        return not self.issue and self.prefix_kwh is not None and self.physical_available

    @property
    def reference_version(self):
        """Exclude routine checkpoint and physical updates from confirmation concurrency."""
        return digest(
            [self.reference_at, [asdict(item) | {"latest_sum": None} for item in self.series]]
        )

    def used(self, now):
        """Return the confirmed prefix plus nonnegative component exports in kWh.

        Each negative endpoint difference contributes zero export.
        Missing or invalid evidence never becomes an assumed zero volume.

        Raises:
            ValueError: Evidence has a blocking issue, the prefix is unknown,
                or the latest completed endpoint is stale or in the future.
        """
        if self.issue:
            raise ValueError(self.issue)
        if self.prefix_kwh is None:
            raise ValueError("bonus_statistics_prefix_required")
        if not timedelta(0) <= now - self.latest_at <= STATISTICS_MAX_AGE:
            raise ValueError("bonus_statistics_expired")
        return self.prefix_kwh + sum(item.exported_kwh for item in self.series)

    def document(self):
        """Serialize aware timestamps explicitly for SQL documents and HTTP JSON."""
        return {
            **asdict(self),
            "series": [asdict(item) for item in self.series],
            "reference_at": self.reference_at.isoformat(),
            "latest_at": self.latest_at.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }

    @classmethod
    def parse(cls, body):
        """Decode a strict persisted basis without accepting unknown fields or identities."""
        if not isinstance(body, dict) or set(body) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid statistics basis fields")
        data = dict(body)
        for key in ("reference_at", "latest_at", "confirmed_at"):
            data[key] = datetime.fromisoformat(data[key]) if data[key] is not None else None
        data["series"] = tuple(StatisticReference(**item) for item in data["series"])
        return cls(**data)


@dataclass(frozen=True)
class StatisticsChannel:
    """Normalized component series with optional physical state for settlement evidence."""

    identity: StatisticReference
    hours: tuple[tuple[datetime, float, float | None], ...]


def parse_statistics(body, request):
    """Validate a bounded multi-meter read against the app-owned requested interval."""
    if not isinstance(body, dict) or set(body) != {"request_version", "statistics"}:
        raise ValueError("Invalid export statistics fields")
    values = body["statistics"]
    if not isinstance(values, list) or not values:
        raise ValueError("Invalid statistics channel count")
    channels = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "statistic_id",
            "metadata_id",
            "source",
            "unit",
            "hours",
        }:
            raise ValueError("Invalid statistics channel fields")
        rows = value["hours"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_HISTORY_HOURS:
            raise ValueError("Invalid statistics row count")
        hours = []
        for row in rows:
            if not isinstance(row, list) or len(row) != 3 or type(row[0]) not in (float, int):
                raise ValueError("Invalid statistics row")
            instant = datetime.fromtimestamp(row[0], UTC)
            if (
                instant.minute
                or instant.second
                or instant.microsecond
                or not min(start for _, start in request.periods) - STATISTICS_HOUR
                <= instant
                < request.end
            ):
                raise ValueError("Statistics outside requested hour boundaries")
            total = energy(row[1])
            state = energy(row[2]) if row[2] is not None else None
            if state is not None and state < 0:
                raise ValueError("Invalid physical export state")
            hours.append((instant, total, state))
        if any(before[0] >= after[0] for before, after in pairwise(hours)):
            raise ValueError("Unordered export statistics")
        identity = StatisticReference(
            **{key: value[key] for key in ("statistic_id", "metadata_id", "source", "unit")},
            reference_sum=hours[0][1],
            latest_sum=hours[-1][1],
        )
        channels.append(StatisticsChannel(identity, tuple(hours)))
    if sorted(channel.identity.statistic_id for channel in channels) != sorted(request.entity_ids):
        raise ValueError("Export statistics membership changed")
    return tuple(sorted(channels, key=lambda item: item.identity.statistic_id))


def derive_statistics(request, channels, book, now):
    """Advance endpoints while retaining the fixed reference and confirmed prefix.

    Intermediate hourly gaps and corrections do not change endpoint arithmetic.
    Missing endpoints and changed reference identities still fail closed.

    Returns:
        A counter book with validated checkpoints. Failed revalidation retains
        an existing statistics checkpoint with a blocking issue and rejected
        evidence digest. Unchanged seeds return the original book object.

    Raises:
        ValueError: A requested period lacks valid endpoints and has no
            existing statistics checkpoint to retain. No partial book is returned.
    """
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed

    active = set(request.active_periods)
    seeds = {
        (seed.rule_id, seed.period_start): seed
        for seed in (book.seeds if book else ())
        if (seed.rule_id, seed.period_start) in active and seed.entity_id == request.entity_id
    }
    for rule_id, start in request.periods:
        previous = seeds.get((rule_id, start))
        old = previous.statistics if previous else None
        try:
            maps = [{row[0]: row for row in channel.hours} for channel in channels]
            first = (
                old.reference_at - STATISTICS_HOUR
                if old
                else min(
                    (
                        instant
                        for instant in set.intersection(*(set(rows) for rows in maps))
                        if instant >= start - STATISTICS_HOUR
                    ),
                    default=request.end,
                )
            )
            last = request.end - STATISTICS_HOUR
            if last < first:
                raise ValueError("No completed statistics endpoint")
            selected = []
            for rows in maps:
                selected.append((rows[first], rows[last]))
            identities = tuple(
                replace(channel.identity, reference_sum=points[0][1], latest_sum=points[-1][1])
                for channel, points in zip(channels, selected, strict=True)
            )

            endpoint_evidence = [
                [
                    asdict(channel.identity) | {"reference_sum": None, "latest_sum": None},
                    points,
                ]
                for channel, points in zip(channels, selected, strict=True)
            ]

            if old and (
                old.reference_at != first + STATISTICS_HOUR
                or tuple(replace(item, latest_sum=item.reference_sum) for item in old.series)
                != tuple(replace(item, latest_sum=item.reference_sum) for item in identities)
                or request.end < old.latest_at
            ):
                raise ValueError("Retained export statistics changed")
            physical = all(points[-1][2] is not None for points in selected)
            basis = StatisticsBasis(
                first + STATISTICS_HOUR,
                request.end,
                identities,
                old.prefix_kwh if old else (0.0 if first + STATISTICS_HOUR == start else None),
                old.confirmed_at if old else None,
                digest(endpoint_evidence),
                physical,
            )
            components = (
                tuple(
                    (channel.identity.statistic_id, points[-1][2])
                    for channel, points in zip(channels, selected, strict=True)
                )
                if physical
                else ()
            )
            seeds[rule_id, start] = CounterSeed(
                rule_id,
                start,
                basis.used(now) if basis.prefix_kwh is not None else None,
                sum(value for _, value in components) if physical else None,
                request.entity_id,
                request.end,
                components,
                basis,
            )
        except (ValueError, KeyError):
            if old is None:
                raise ValueError("Incomplete export statistics") from None
            seeds[rule_id, start] = replace(
                previous,
                statistics=replace(
                    old,
                    issue="bonus_statistics_revalidation_required",
                    rejected_digest=digest([asdict(channel) for channel in channels]),
                ),
            )
    result = tuple(seeds.values())
    return book if book is not None and result == book.seeds else CounterBook(now, result)


def invalidate_statistics(book, request, body, now):
    """Retain checkpoints and archive rejected-report provenance after transport validation fails."""
    if book is None:
        return None
    result = replace(
        book,
        recorded_at=now,
        seeds=tuple(
            replace(
                seed,
                statistics=replace(
                    seed.statistics,
                    issue="bonus_statistics_revalidation_required",
                    rejected_digest=digest(
                        {key: value for key, value in body.items() if key != "request_version"}
                    ),
                ),
            )
            if seed.statistics and (seed.rule_id, seed.period_start) in request.periods
            else seed
            for seed in book.seeds
        ),
    )
    return book if result.seeds == book.seeds else result
