"""Validated confirmed bonus-counter versions in the shared active database."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta

from open_energy_planner.contracts.wire import counter_document, parse_counters
from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.history.model import (
    COMPONENT_METER_TOLERANCE_KWH,
    MAX_HISTORY_HOURS,
    EnergyHour,
)

ARCHIVE_HASH_LENGTH = 64


class CounterStore:
    """Reuse the atomic archive writer, with a separate document namespace and strict counter schema."""

    def __init__(self, documents):
        """Load confirmed seeds without inventing an unused bonus budget after restart."""
        self.documents = documents
        self.current_name = "current.json"
        self.current, self.issue = None, None
        try:
            if self.documents.exists(self.current_name):
                self.current = self.load(self.documents.read(self.current_name))
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "bonus_counter_cache_unreadable"

    def load(self, version):
        """Validate a referenced seed archive and its exact content hash."""
        if (
            not isinstance(version, str)
            or len(version) != ARCHIVE_HASH_LENGTH
            or any(char not in "0123456789abcdef" for char in version)
        ):
            raise ValueError("Invalid counter reference")
        body = self.documents.read(f"{version}.json")
        if not isinstance(body, dict):
            raise TypeError("Invalid counter archive object")
        recorded = datetime.fromisoformat(body.pop("recorded_at"))
        book = parse_counters(body, recorded)
        if book.version != version:
            raise ValueError("Counter archive hash mismatch")
        return book

    def provenance(self, seed):
        """Read optional immutable origin evidence. Legacy or unreadable origins remain unknown.

        Provenance never changes seed identity or controls budget arithmetic.
        Keeping it separate preserves existing archive hashes and plan references.
        """
        try:
            name, identity = self._origin_identity(seed)
            if not self.documents.exists(name):
                return None
            value = self.documents.read(name)
            if value["seed"] != identity or value["source"] not in (
                "home_assistant_history",
                "manual_confirmation",
            ):
                return None
            recorded = datetime.fromisoformat(value["recorded_at"])
            if recorded.utcoffset() is None or recorded < seed.observed_at:
                return None
            if seed.statistics:
                if value.get("statistics") != seed.statistics.document():
                    return None
                return value
            if value["source"] == "home_assistant_history":
                start = datetime.fromisoformat(value["history_start"])
                end = datetime.fromisoformat(value["history_end"])
                anchor = datetime.fromisoformat(value["anchor_hour"])
                if (
                    start != seed.period_start
                    or end != seed.observed_at
                    or anchor != start - timedelta(hours=1)
                    or type(value["hourly_rows"]) is not int
                    or not 1 <= value["hourly_rows"] <= MAX_HISTORY_HOURS
                    or value["hourly_rows"] != (end - anchor).total_seconds() / SECONDS_PER_HOUR
                ):
                    return None
                EnergyHour(anchor, value["start_sum_kwh"], seed.meter_kwh)
                EnergyHour(end - timedelta(hours=1), value["end_sum_kwh"], seed.meter_kwh)
                if (
                    abs(value["end_sum_kwh"] - value["start_sum_kwh"] - seed.used_kwh)
                    > COMPONENT_METER_TOLERANCE_KWH
                ):
                    return None
                if any(
                    not isinstance(value[key], str)
                    or len(value[key]) != ARCHIVE_HASH_LENGTH
                    or any(char not in "0123456789abcdef" for char in value[key])
                    for key in ("request_version", "history_digest")
                ):
                    return None
            return value
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _origin_identity(self, seed):
        """Bind explanatory evidence to exact seed contents, independently of changing books."""
        identity = json.loads(json.dumps(asdict(seed), default=str, sort_keys=True))
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return f"{digest}.origin.json", identity

    def save_history(self, book, *, request, history):
        """Archive verified hourly endpoints for newly derived openings.

        Args:
            book: Derived counter book, possibly retaining unrelated existing seeds.
            request: Validated history request identifying the newly derived periods.
            history: Complete normalized hourly cumulative statistics in kWh.

        Raises:
            ValueError: Missing coverage, invalid boundaries or a statistics difference
                that does not reproduce the opening's used kWh.
            OSError: Archive reading or transactional writing failed.

        Existing origins remain immutable. Save the book, new origins and pointer
        together through save after validating the history difference.
        """
        origins = []
        for seed in book.seeds:
            if (seed.rule_id, seed.period_start) not in request.periods:
                continue
            rows = tuple(
                hour
                for hour in history.hours
                if seed.period_start - timedelta(hours=1) <= hour.start < request.end
            )
            volume, _meter = history.baseline(seed.period_start, request.end)
            if abs(seed.used_kwh - volume) > COMPONENT_METER_TOLERANCE_KWH:
                raise ValueError("History provenance differs from seed")
            origins.append(
                (
                    seed,
                    {
                        "source": "home_assistant_history",
                        "recorded_at": book.recorded_at.isoformat(),
                        "history_start": seed.period_start.isoformat(),
                        "history_end": request.end.isoformat(),
                        "anchor_hour": rows[0].start.isoformat(),
                        "hourly_rows": len(rows),
                        "start_sum_kwh": rows[0].total_kwh,
                        "end_sum_kwh": rows[-1].total_kwh,
                        "request_version": request.version,
                        "history_digest": hashlib.sha256(
                            json.dumps(asdict(history), default=str, sort_keys=True).encode()
                        ).hexdigest(),
                    },
                )
            )
        self.save(book, origins=origins)

    def save_confirmation(self, book, *, seed):
        """Record a user-confirmed opening without attributing its volume to HA history.

        Args:
            book: Counter book containing the newly confirmed opening.
            seed: Confirmed used kWh and matching HA physical meter/sample instant.

        Raises:
            ValueError: The seed does not belong to the book or an existing archive
                fails validation.
            OSError: Archive reading or transactional writing failed.

        Delegate the atomic book, optional-origin and pointer write to save.
        """
        self.save(
            book,
            origins=(
                (
                    seed,
                    {"source": "manual_confirmation", "recorded_at": book.recorded_at.isoformat()},
                ),
            ),
        )

    def save_statistics(self, book):
        """Atomically archive sum references, confirmations and predecessor provenance."""
        self.save(
            book,
            origins=tuple(
                (
                    seed,
                    {
                        "source": "manual_confirmation"
                        if seed.statistics.confirmed_at
                        else "home_assistant_history",
                        "recorded_at": book.recorded_at.isoformat(),
                        "statistics": seed.statistics.document(),
                        "supersedes": self.current.version if self.current else None,
                    },
                )
                for seed in book.seeds
                if seed.statistics
            ),
        )

    def save(self, book, *, origins=()):
        """Preserve an immutable opening book and optional origins before publishing its pointer.

        Args:
            book: Validated immutable counter book to publish.
            origins: Pairs of seeds in book.seeds and app-owned origin documents.
                Existing origin documents are preserved, never overwritten.

        Raises:
            ValueError: An origin seed is absent from the book or an existing
                archive fails identity/content validation.
            OSError: Archive reading or transactional writing failed.

        Book, new origins and current pointer share one document transaction.
        Exceptions raised inside its body roll back those writes. Install in-memory
        state only after the transaction context exits successfully.
        """
        with self.documents.transaction():
            document = counter_document(book)
            for key in ("status", "version"):
                document.pop(key)
            target = f"{book.version}.json"
            if self.documents.exists(target):
                self.load(book.version)
            else:
                self.documents.write(target, document)
            for seed, origin in origins:
                if seed not in book.seeds:
                    raise ValueError("Origin seed is not in the counter book")
                name, identity = self._origin_identity(seed)
                value = {**origin, "seed": identity}
                if not self.documents.exists(name):
                    self.documents.write(name, value)
            self.documents.write(self.current_name, book.version)

        self.current, self.issue = book, None
