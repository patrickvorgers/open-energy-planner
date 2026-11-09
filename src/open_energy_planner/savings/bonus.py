"""Historical bonus evidence from immutable HA frames and bounded native history reads."""

from dataclasses import replace
from datetime import datetime, timedelta
from math import ceil, fsum

from open_energy_planner.contracts.bonus_history import BaselineRequest, BonusHistory
from open_energy_planner.contracts.bonus_statistics import derive_statistics
from open_energy_planner.history.model import COMPONENT_METER_TOLERANCE_KWH, MAX_HISTORY_HOURS
from open_energy_planner.storage.counters import CounterStore

from ..contracts.bonus import MAX_COUNTER_SEEDS, CounterBook
from ..contracts.compilation import BonusCompiler
from ..contracts.export_meter import export_identity
from ..storage.adapters.documents import SqlDocuments
from .model import MAX_BATCH_INTERVALS, SETTLEMENT_INTERVAL, PendingSettlement, parse_frame

MAX_BACKDATED_SEEDS = 64
HISTORY_HOUR_SECONDS = 3600


def _require_physical_statistics(seed):
    """Reject statistics anchors without validated usage and an exact physical endpoint."""
    if seed.statistics and not seed.statistics.physical_settlement_ready:
        raise PendingSettlement("Validated physical bonus endpoint is unavailable")


class ExportCaptureGap(PendingSettlement):
    """Identify the first missing physical interval separately from changed meter evidence."""

    def __init__(self, message: str, start: datetime):
        """Retain the owned waiting explanation and the earliest unproven interval boundary."""
        super().__init__(message)
        self.start = start


class HistoricalBonuses:
    """Keep historical seeds separate from authoritative current planning confirmations.

    The owning savings worker serializes this service and its ingress with the
    settlement mutation lock. All stores use the active app SQLAlchemy engine.
    Derived seed archives never replace current planning counters or raw frames.
    """

    def __init__(self, store):
        """Borrow immutable frames and reuse the validated counter archive for historical HA reads."""
        self.store = store
        self.archive = CounterStore(SqlDocuments(store.engine, "savings-bonus-counters"))
        self.pending = None
        self.issue = None
        self.backdated = {}
        self.statistics_sources = {}

    def backdate(self, seed, start, exports):
        """Subtract measured exports between an older boundary and a confirmed seed.

        Args:
            seed: Confirmed period usage and exact physical meter readings in kWh.
            start: Earlier aware frame boundary in the same bonus period.
            exports: Exact physical export entity IDs of the historical installation.

        Returns:
            A new seed at start. Complete frames use Recorder reset-aware deltas.
            An anchor inside a frame uses its physical counter only when both
            boundaries prove no reset in that particular frame. No uniform split
            or current-state substitution is used. Original evidence is unchanged.

        Raises:
            PendingSettlement: A frame, member, anchor endpoint or continuity proof
                is missing or inconsistent, or the anchor cannot cover this period.
        """
        key = (seed, start, tuple(sorted(exports)))
        _require_physical_statistics(seed)
        if key in self.backdated:
            return self.backdated[key]
        components = dict(seed.component_meters) or {seed.entity_id: seed.meter_kwh}
        if (
            set(components) != set(exports)
            or export_identity(exports) != seed.entity_id
            or not seed.period_start <= start < seed.observed_at
            or seed.observed_at - start > timedelta(hours=MAX_HISTORY_HOURS)
        ):
            raise PendingSettlement(
                "Historical bonus anchor does not match the export meters or period"
            )
        cursor, total, initial, anchor_frame = start, 0.0, None, None
        capture_gap = None
        while cursor <= seed.observed_at + SETTLEMENT_INTERVAL:
            rows = self.store.frames(
                cursor, end=seed.observed_at + 2 * SETTLEMENT_INTERVAL, limit=MAX_BATCH_INTERVALS
            )
            if not rows:
                break
            for row in rows:
                frame = parse_frame(row["frame"])
                if frame.start < cursor:
                    raise PendingSettlement(
                        "Historical bonus export evidence has a gap or changed meters"
                    )
                if frame.start != cursor and capture_gap is None:
                    capture_gap = ExportCaptureGap(
                        "Historical bonus export evidence has a gap or changed meters", cursor
                    )
                if (
                    capture_gap is not None
                    and frame.start > seed.observed_at
                    and (anchor_frame is None or frame.start > anchor_frame.end)
                ):
                    raise capture_gap
                if (
                    set(row["context"]["bindings"]["exports"]) != set(exports)
                    or not set(exports) <= frame.meter_start.keys()
                ):
                    raise PendingSettlement(
                        "Historical bonus export evidence has a gap or changed meters"
                    )
                if capture_gap is not None:
                    if frame.start >= seed.observed_at:
                        raise capture_gap
                    if seed.observed_at < frame.end:
                        anchor_frame = frame
                    cursor = frame.end
                    continue
                if initial is None:
                    initial = {entity: frame.meter_start[entity] for entity in exports}
                if anchor_frame is not None:
                    for entity in exports:
                        expected_end = (
                            anchor_frame.meter_start[entity] + anchor_frame.channels[entity]
                        )
                        if (
                            abs(frame.meter_start[entity] - expected_end)
                            > COMPONENT_METER_TOLERANCE_KWH
                        ):
                            raise PendingSettlement(
                                "A reset inside the bonus anchor interval prevents exact reconstruction"
                            )
                        partial = components[entity] - anchor_frame.meter_start[entity]
                        if (
                            not -COMPONENT_METER_TOLERANCE_KWH
                            <= partial
                            <= anchor_frame.channels[entity] + COMPONENT_METER_TOLERANCE_KWH
                        ):
                            raise PendingSettlement(
                                "Confirmed bonus meter is outside its historical interval"
                            )
                        total += min(anchor_frame.channels[entity], max(0, partial))
                    break
                if seed.observed_at == frame.start:
                    if any(
                        abs(components[entity] - frame.meter_start[entity])
                        > COMPONENT_METER_TOLERANCE_KWH
                        for entity in exports
                    ):
                        raise PendingSettlement(
                            "Confirmed bonus meter disagrees with its historical boundary"
                        )
                    break
                if seed.observed_at < frame.end:
                    anchor_frame = frame
                else:
                    total += fsum(frame.channels[entity] for entity in exports)
                cursor = frame.end
            else:
                continue
            used = seed.used_kwh - total
            if used < -COMPONENT_METER_TOLERANCE_KWH:
                raise PendingSettlement("Historical exports exceed the confirmed used bonus volume")
            result = replace(
                seed,
                used_kwh=max(0, used),
                observed_at=start,
                meter_kwh=fsum(initial.values()),
                component_meters=tuple(sorted(initial.items())),
                # This derived physical endpoint is separate from the archived sum reference.
                statistics=None,
            )
            if len(self.backdated) >= MAX_BACKDATED_SEEDS:
                self.backdated.clear()
            self.backdated[key] = result
            return result
        if capture_gap is not None:
            raise capture_gap
        raise ExportCaptureGap("Historical bonus baseline awaits captured export evidence", cursor)

    def prepare(self, contract, frame, bindings, seeds, book, now, revision, previous=None):
        """Prepare exact beginning usage, or request missing older periods from HA.

        Current confirmations override archived context and historical automatic
        seeds for the same source/period. An incompatible or incomplete anchor is
        never replaced by zero. Missing periods use the existing hourly-history
        protocol, ending at the next completed hour around the historical frame.
        """
        self.pending = None
        if not contract.bonuses:
            self.issue = None
            return ()
        if self.archive.issue:
            raise PendingSettlement("Historical bonus counter archive is unreadable")
        candidates = {
            (seed.rule_id, seed.period_start, seed.entity_id): seed
            for seed in (
                *((self.archive.current.seeds) if self.archive.current else ()),
                *seeds,
                *((book.seeds) if book else ()),
            )
        }
        exports = bindings["exports"]
        identity = export_identity(exports)
        result, missing, statistics_missing = [], [], []
        historical = {
            (seed.rule_id, seed.period_start, seed.entity_id): seed
            for seed in (self.archive.current.seeds if self.archive.current else ())
        }
        endpoint = datetime.fromtimestamp(
            ceil(frame.start.timestamp() / HISTORY_HOUR_SECONDS) * HISTORY_HOUR_SECONDS,
            frame.start.tzinfo,
        )
        for family, versions in BonusCompiler().families(contract).items():
            policy = versions[0]
            if frame.start < policy.effective_from:
                continue
            if policy.contract_start is not None and frame.start < policy.contract_start:
                continue
            period = policy.period_start(frame.start)
            counter_key = family + "|" + period.isoformat()
            if previous and all(
                counter_key in previous["counters"][kind] for kind in ("actual", "reference")
            ):
                continue
            seed = candidates.get((family, period, identity))
            if seed is None:
                if period < frame.start:
                    missing.append((family, period))
                continue
            _require_physical_statistics(seed)
            if (
                seed.statistics
                and book is not None
                and seed in book.seeds
                and seed.observed_at > frame.start
            ):
                cached = historical.get((family, period, identity))
                if (
                    book is None
                    or cached is None
                    or self.statistics_sources.get(cached) != book.version
                    or cached.observed_at != endpoint
                ):
                    try:
                        result.append(self.backdate(seed, frame.start, exports))
                        continue
                    except ExportCaptureGap as error:
                        if error.start <= endpoint:
                            raise
                    if endpoint < seed.statistics.reference_at:
                        raise PendingSettlement(
                            "Historical bonus interval predates the confirmed statistics reference"
                        )
                    statistics_missing.append((family, period))
                    continue
                _require_physical_statistics(cached)
                seed = cached
            result.append(
                self.backdate(seed, frame.start, exports)
                if seed.observed_at > frame.start
                else seed
            )
        if missing or statistics_missing:
            end = endpoint
            missing = statistics_missing or missing
            # A sum reference row already proves the exact period-start endpoint.
            # Hourly-delta history still requires a completed following hour.
            if not statistics_missing and end <= min(period for _, period in missing):
                end += timedelta(hours=1)
            if end > now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1):
                raise PendingSettlement(
                    "Historical bonus baseline awaits completed hourly statistics"
                )
            periods = tuple(sorted(set(missing)))
            if any(period.timestamp() % HISTORY_HOUR_SECONDS for _, period in periods):
                raise PendingSettlement(
                    "Historical bonus period does not start on a whole UTC hour. "
                    "Confirm a manual baseline"
                )
            self.pending = BaselineRequest(
                contract.version,
                revision,
                book.version if book else None,
                identity,
                periods,
                periods,
                end,
                tuple(sorted(exports)),
                bool(statistics_missing),
            )
            raise PendingSettlement(
                self.issue or "Historical bonus baseline is being read from Home Assistant"
            )
        self.issue = None
        return tuple(result)

    def receive(self, request, history, now):
        """Retain historical periods within the shared book limit, always keeping requested seeds.

        Newly requested older periods must survive even when all cached periods
        are newer. Otherwise a replay could repeat the same annual request forever.
        Evicted books remain immutable in the database. Current planning counters
        are never changed by this historical cache.
        """
        book = BonusHistory().derive(request, history, None, now)
        kept = self._retain_other_periods(book.seeds)
        self.archive.save_history(CounterBook(now, kept), request=request, history=history)
        self.pending, self.issue = None, None

    def receive_statistics(self, request, channels, book, now):
        """Archive a historical endpoint against the current validated reference and correction.

        Request versions prevent delayed reports from replacing a newer confirmation.
        A new live counter version or restart revalidates the historical checkpoint.
        Neither current planning counters nor original measurement frames are changed.

        Args:
            request: App-owned historical request with aware period bounds, export
                meter identities and the expected current counter-book version.
            channels: One normalized statistics series per requested export meter.
                Cumulative sums and available physical meter readings are in kWh.
            book: Current counter book whose version must match the request and
                whose references and confirmed missing-period prefixes remain
                authoritative for the derived historical endpoints.
            now: Aware timestamp recorded on the historical archive book.

        Raises:
            PendingSettlement: A current or derived statistics seed lacks a known
                prefix or physical endpoint, or requires reference revalidation.
            ValueError: The book is missing or changed, a requested reference is
                unavailable or changed, or endpoint/counter/archive validation fails.
            OSError: Reading the archive or its transactional write fails.

        Side effects:
            The caller serializes this operation with the settlement mutation lock.
            Requested seeds are validated before archival. CounterStore commits
            the historical book, new origins and current pointer in one transaction.
            After successful archival, this method updates statistics_sources in
            process memory and clears pending and issue. It does not roll back a
            committed archive if a subsequent process-memory update fails.
        """
        if book is None or book.version != request.counter_version:
            raise ValueError("Historical bonus counter changed")
        current = {
            (seed.rule_id, seed.period_start): seed
            for seed in book.seeds
            if seed.entity_id == request.entity_id
        }
        seeds = []
        for key in request.periods:
            seed = current.get(key)
            if seed is None or seed.statistics is None:
                raise ValueError("Historical bonus statistics reference is unavailable")
            _require_physical_statistics(seed)
            basis = seed.statistics
            selected = tuple(
                replace(
                    channel,
                    hours=tuple(
                        row
                        for row in channel.hours
                        if row[0] >= basis.reference_at - timedelta(hours=1)
                    ),
                )
                for channel in channels
            )
            historical = derive_statistics(
                replace(request, periods=(key,), active_periods=(key,)),
                selected,
                None,
                request.end,
            ).seeds[0]
            if historical.statistics.reference_version != basis.reference_version:
                raise ValueError("Historical bonus statistics reference changed")
            statistics = replace(
                historical.statistics, prefix_kwh=basis.prefix_kwh, confirmed_at=basis.confirmed_at
            )
            historical = replace(
                historical,
                statistics=statistics,
                used_kwh=basis.prefix_kwh + sum(item.exported_kwh for item in statistics.series),
            )
            _require_physical_statistics(historical)
            seeds.append(historical)
        # The immutable archive keeps provenance. Only this process remembers validation
        # against a live counter version, so restart safely requests fresh HA evidence.
        saved = CounterBook(now, self._retain_other_periods(seeds))
        self.archive.save_statistics(saved)
        self.statistics_sources = {
            seed: self.statistics_sources[seed]
            for seed in saved.seeds
            if seed in self.statistics_sources
        } | {seed: book.version for seed in seeds}
        self.pending, self.issue = None, None

    def _retain_other_periods(self, seeds):
        """Keep requested periods and the most recent other anchors within the existing archive bound."""
        requested = {(seed.rule_id, seed.period_start) for seed in seeds}
        retained = tuple(
            seed
            for seed in (self.archive.current.seeds if self.archive.current else ())
            if (seed.rule_id, seed.period_start) not in requested
        )
        available = MAX_COUNTER_SEEDS - len(seeds)
        kept = (
            tuple(sorted(retained, key=lambda seed: seed.observed_at)[-available:])
            if available
            else ()
        )
        return (*kept, *seeds)
