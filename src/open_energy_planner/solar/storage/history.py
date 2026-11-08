"""Resume bounded solar-history transfers while retaining all available measurement years."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from open_energy_planner.history.model import EnergyHistory
from open_energy_planner.history.wire import parse_history
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import version

HISTORY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
MAX_REQUEST_DAYS = 367
DISCOVERY_BASIS = "solar_history_range"
CHUNK_BASIS = "solar_history_chunk"


class HistoryContinuityError(ValueError):
    """Retain the last complete cache when source identity or earlier measurements change."""


@dataclass(frozen=True)
class SolarHistoryReport:
    """One validated availability envelope or bounded hourly transfer from Recorder."""

    basis: str
    entity_id: str
    statistics_identity: dict
    history: EnergyHistory | None = None
    available_start: datetime | None = None


def _statistics_identity(value):
    """Bind cached rows to a Recorder metadata identity and its original energy unit."""
    if (
        not isinstance(value, dict)
        or set(value) != {"metadata_id", "source", "unit"}
        or type(value["metadata_id"]) is not int
        or value["metadata_id"] < 0
        or not isinstance(value["source"], str)
        or not 1 <= len(value["source"]) <= 128
        or value["unit"] not in ("Wh", "kWh", "MWh")
    ):
        raise ValueError("Invalid solar statistics identity")
    return dict(value)


def parse_report(body):
    """Parse solar-only availability or hourly responses without expanding shared wire limits."""
    if not isinstance(body, dict):
        raise TypeError("Invalid solar history report")
    common = {"request_version", "basis", "entity_id", "statistics_identity"}
    basis = body.get("basis")
    expected = common | ({"available_start"} if basis == DISCOVERY_BASIS else {"unit", "hours"})
    if basis not in (DISCOVERY_BASIS, CHUNK_BASIS) or set(body) != expected:
        raise ValueError("Invalid solar history fields")
    if (
        not isinstance(body["request_version"], str)
        or not isinstance(body["entity_id"], str)
        or not body["entity_id"].startswith("sensor.")
    ):
        raise ValueError("Invalid solar history source")
    identity = _statistics_identity(body["statistics_identity"])
    if basis == DISCOVERY_BASIS:
        first = body["available_start"]
        if first is not None:
            if not isinstance(first, str):
                raise ValueError("Invalid solar availability start")
            first = aware(datetime.fromisoformat(first))
            if first < HISTORY_EPOCH or first.minute or first.second or first.microsecond:
                raise ValueError("Invalid solar availability boundary")
        return SolarHistoryReport(basis, body["entity_id"], identity, available_start=first)
    compact = {
        key: value for key, value in body.items() if key not in {"basis", "statistics_identity"}
    }
    if body["hours"] == [] and body["unit"] == "kWh":
        history = None
    else:
        history = parse_history(compact)
    return SolarHistoryReport(basis, body["entity_id"], identity, history=history)


class SolarHistoryImport:
    """Keep immutable annual chunks and a resumable source-bound current import pointer.

    Annual Recorder transfers remain bounded. The in-memory aggregate has no
    functional year limit. Empty years retain explicit completed coverage and
    cannot truncate later years. Changes to previously accepted rows or source
    identity retain original evidence and block replacement of the current cache.
    """

    def __init__(self, store):
        """Borrow the existing solar database namespace without adding tables or weather ownership."""
        self.store = store
        self.states = {}
        self.issues = {}
        self.learning_starts = {}

    def configure(self, sources):
        """Bind mutable pointers to each explicitly chosen production regime.

        Clearing a date selects the original meter cohort again, including any
        original quarantine. Immutable chunks from earlier cohorts stay archived.
        """
        starts = {source.entity_id: source.learning_start for source in sources}
        self.states = {
            entity: state
            for entity, state in self.states.items()
            if entity in starts and self.learning_starts.get(entity) == starts[entity]
        }
        self.issues = {
            entity: issue
            for entity, issue in self.issues.items()
            if entity in starts and self.learning_starts.get(entity) == starts[entity]
        }
        self.learning_starts = starts

    def _cohort_key(self, entity):
        """Keep default meter pointers stable and bind explicit dates to separate pointer families."""
        start = self.learning_starts.get(entity)
        return version({"entity_id": entity, "learning_start": start}) if start else version(entity)

    def _cohort_fields(self, entity):
        """Record an explicit regime boundary in durable pointer targets without changing defaults."""
        start = self.learning_starts.get(entity)
        return {"learning_start": start} if start else {}

    def _first_endpoint(self, entity):
        """Keep one cumulative endpoint before the first complete UTC hour in the selected regime."""
        start = self.learning_starts.get(entity)
        if start is None:
            return HISTORY_EPOCH
        boundary = datetime.fromisoformat(start)
        hour = boundary.replace(minute=0, second=0, microsecond=0)
        first_hour = hour + HOUR if boundary > hour else hour
        return max(HISTORY_EPOCH, first_hour - HOUR)

    def _quarantine(self, entity, kind, reference):
        """Persist a blocking pointer to original conflicting evidence before any cache can be reused."""
        fault = {
            "entity_id": entity,
            **self._cohort_fields(entity),
            "issue": "history_continuity_unavailable",
            "incoming_kind": kind,
            "incoming_reference": reference,
        }
        fault_reference = version(fault)
        with self.store.documents.transaction():
            self.store.archive("history-import-fault", fault_reference, fault)
            self.store.documents.write(
                "history-import-quarantine-" + self._cohort_key(entity) + ".json", fault_reference
            )
        self.issues[entity] = "history_continuity_unavailable"

    def _restore_quarantine(self, entity):
        """Retain known source faults across restart without discarding their last accepted measurements."""
        pointer = "history-import-quarantine-" + self._cohort_key(entity) + ".json"
        if not self.store.documents.exists(pointer):
            return
        reference = self.store.documents.read(pointer)
        if reference is None:
            self.issues.pop(entity, None)
            return
        if not isinstance(reference, str) or len(reference) != 64:
            raise ValueError("Invalid solar history quarantine")
        fault = self.store.documents.read("history-import-fault-" + reference + ".json")
        if (
            not isinstance(fault, dict)
            or version(fault) != reference
            or set(fault)
            != {"entity_id", "issue", "incoming_kind", "incoming_reference"}
            | set(self._cohort_fields(entity))
            or fault["entity_id"] != entity
            or fault.get("learning_start") != self.learning_starts.get(entity)
            or fault["issue"] != "history_continuity_unavailable"
            or fault["incoming_kind"] not in ("history-import-chunk", "history-import-discovery")
            or not isinstance(fault["incoming_reference"], str)
            or len(fault["incoming_reference"]) != 64
        ):
            raise ValueError("Invalid solar history quarantine")
        self.issues[entity] = fault["issue"]

    def _state(self, entity):
        """Load and validate the source's exact immutable import state once per runtime."""
        if entity in self.states:
            return self.states[entity]
        pointer = "history-import-current-" + self._cohort_key(entity) + ".json"
        state = None
        if self.store.documents.exists(pointer):
            reference = self.store.documents.read(pointer)
            if (
                not isinstance(reference, str)
                or len(reference) != 64
                or any(character not in "0123456789abcdef" for character in reference)
            ):
                raise ValueError("Invalid solar history pointer")
            state = self.store.documents.read("history-import-" + reference + ".json")
            if (
                not isinstance(state, dict)
                or set(state)
                != {
                    "entity_id",
                    "statistics_identity",
                    "available_start",
                    "discovered_at",
                    "chunks",
                }
                | set(self._cohort_fields(entity))
                or state["entity_id"] != entity
                or state.get("learning_start") != self.learning_starts.get(entity)
                or version(state) != reference
                or not isinstance(state["chunks"], list)
            ):
                raise ValueError("Invalid solar history import")
            _statistics_identity(state["statistics_identity"])
            aware(datetime.fromisoformat(state["discovered_at"]))
            if state["available_start"] is not None:
                first = aware(datetime.fromisoformat(state["available_start"]))
                if first < HISTORY_EPOCH:
                    raise ValueError("Invalid solar history discovery")
            years = [chunk["year"] for chunk in state["chunks"]]
            if len(set(years)) != len(years) or any(
                type(year) is not int or year < 1970 for year in years
            ):
                raise ValueError("Invalid solar history years")
        self.states[entity] = state
        return state

    def _chunk(self, state, descriptor):
        """Verify a bounded immutable chunk before admitting any cached measurement."""
        body = self.store.documents.read(
            "history-import-chunk-" + descriptor["reference"] + ".json"
        )
        if (
            not isinstance(body, dict)
            or set(body) != {"entity_id", "statistics_identity", "year", "start", "end", "hours"}
            or version(body) != descriptor["reference"]
            or body["entity_id"] != state["entity_id"]
            or body["statistics_identity"] != state["statistics_identity"]
            or any(body[field] != descriptor[field] for field in ("year", "start", "end"))
        ):
            raise ValueError("Invalid cached solar history chunk")
        start, end = (aware(datetime.fromisoformat(body[field])) for field in ("start", "end"))
        if not timedelta(0) < end - start <= timedelta(days=MAX_REQUEST_DAYS):
            raise ValueError("Invalid cached solar history range")
        report = parse_report(
            {
                "request_version": "cached",
                "basis": CHUNK_BASIS,
                "entity_id": body["entity_id"],
                "statistics_identity": body["statistics_identity"],
                "unit": "kWh",
                "hours": body["hours"],
            }
        )
        if report.history and any(not start <= hour.start < end for hour in report.history.hours):
            raise ValueError("Invalid cached solar history coverage")
        return report.history

    def _history(self, state):
        """Merge matching overlapping endpoints while preserving every absent hour as a gap."""
        rows = {}
        for descriptor in state["chunks"]:
            history = self._chunk(state, descriptor)
            for hour in history.hours if history else ():
                if hour.start in rows and rows[hour.start] != hour:
                    raise HistoryContinuityError("Solar history overlap changed")
                rows[hour.start] = hour
        return (
            EnergyHistory(state["entity_id"], tuple(rows[key] for key in sorted(rows)))
            if rows
            else None
        )

    def restore(self, entities):
        """Restore complete accepted chunks for selected meter identities and isolate cache faults."""
        histories = {}
        for entity in entities:
            try:
                state = self._state(entity)
                history = self._history(state) if state else None
                if history:
                    histories[entity] = history
                self._restore_quarantine(entity)
            except (OSError, ValueError, TypeError, KeyError):
                self.issues[entity] = "history_cache_unreadable"
        return histories

    def status(self, entities, histories):
        """Explain discovered monthly coverage and accepted annual chunks without exposing row payloads."""
        result = {}
        for entity in entities:
            state = self.states.get(entity)
            chunks = state["chunks"] if state else []
            result[entity] = {
                "available_from_month": state["available_start"] if state else None,
                "loaded_years": [chunk["year"] for chunk in chunks],
                "loaded_through": max((chunk["end"] for chunk in chunks), default=None),
                "hours": len(histories[entity].hours) if entity in histories else 0,
                "issue": self.issues.get(entity),
            }
        return result

    def request(self, entity, configuration_version, now):
        """Discover available months once, then request each missing or current annual UTC chunk."""
        if self.issues.get(entity) == "history_cache_unreadable":
            return None
        now = aware(now).astimezone(UTC)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) - HOUR
        state = self._state(entity)
        if state is None or state["available_start"] is None:
            if state and datetime.fromisoformat(state["discovered_at"]).date() == now.date():
                return None
            body = {
                "basis": DISCOVERY_BASIS,
                "entity_id": entity,
                "start": HISTORY_EPOCH.isoformat(),
                "end": target.isoformat(),
                "configuration_version": configuration_version,
            }
            return {**body, "version": version(body)}
        first = datetime.fromisoformat(state["available_start"])
        first_endpoint = self._first_endpoint(entity)
        first = max(first, first_endpoint + HOUR if self.learning_starts.get(entity) else first)
        completed = {chunk["year"]: chunk for chunk in state["chunks"]}
        for year in range(first.year, target.year + 1):
            start = max(first_endpoint, datetime(year, 1, 1, tzinfo=UTC) - HOUR)
            end = min(datetime(year + 1, 1, 1, tzinfo=UTC), target)
            if end <= start:
                continue
            if (
                year in completed
                and completed[year]["start"] == start.isoformat()
                and completed[year]["end"] == end.isoformat()
            ):
                continue
            body = {
                "basis": CHUNK_BASIS,
                "entity_id": entity,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "import_year": year,
                "configuration_version": configuration_version,
            }
            return {**body, "version": version(body)}
        return None

    def receive(self, request, report, now):
        """Archive a validated transfer before advancing its pointer or replacing accepted measurements."""
        entity = request["entity_id"]
        if (
            not isinstance(report, SolarHistoryReport)
            or report.entity_id != entity
            or report.basis != request["basis"]
        ):
            raise ValueError("Solar history source/range mismatch")
        identity = _statistics_identity(report.statistics_identity)
        start, end = (aware(datetime.fromisoformat(request[field])) for field in ("start", "end"))
        state = self._state(entity)
        if request["basis"] == DISCOVERY_BASIS:
            if report.available_start is not None and not start <= report.available_start < end:
                raise ValueError("Solar availability outside requested range")
            discovery = {
                "entity_id": entity,
                **self._cohort_fields(entity),
                "statistics_identity": identity,
                "available_start": report.available_start.isoformat()
                if report.available_start
                else None,
                "captured_at": aware(now).isoformat(),
            }
            discovery_reference = version(discovery)
            self.store.archive("history-import-discovery", discovery_reference, discovery)
            if state and identity != state["statistics_identity"]:
                self._quarantine(entity, "history-import-discovery", discovery_reference)
                raise HistoryContinuityError("Solar statistics identity changed")
            available = [
                value
                for value in (
                    state["available_start"] if state else None,
                    report.available_start.isoformat() if report.available_start else None,
                )
                if value is not None
            ]
            proposed = {
                "entity_id": entity,
                **self._cohort_fields(entity),
                "statistics_identity": identity,
                "available_start": min(available) if available else None,
                "discovered_at": aware(now).isoformat(),
                "chunks": list(state["chunks"]) if state else [],
            }
            history = self._history(proposed)
        else:
            if (
                state is None
                or report.history
                and any(not start <= hour.start < end for hour in report.history.hours)
            ):
                raise ValueError("Solar history source/range mismatch")
            body = {
                "entity_id": entity,
                "statistics_identity": identity,
                "year": request["import_year"],
                "start": request["start"],
                "end": request["end"],
                "hours": [
                    [hour.start.timestamp(), hour.total_kwh, hour.meter_kwh]
                    for hour in report.history.hours
                ]
                if report.history
                else [],
            }
            reference = version(body)
            self.store.archive("history-import-chunk", reference, body)
            if identity != state["statistics_identity"]:
                self._quarantine(entity, "history-import-chunk", reference)
                raise HistoryContinuityError("Solar statistics identity changed")
            previous = next(
                (chunk for chunk in state["chunks"] if chunk["year"] == body["year"]), None
            )
            if previous:
                old = self._chunk(state, previous)
                fresh = (
                    {hour.start: hour for hour in report.history.hours} if report.history else {}
                )
                if old and any(
                    fresh.get(hour.start) != hour for hour in old.hours if start <= hour.start < end
                ):
                    self._quarantine(entity, "history-import-chunk", reference)
                    raise HistoryContinuityError("Cached solar measurements changed")
            descriptor = {field: body[field] for field in ("year", "start", "end")}
            descriptor["reference"] = reference
            proposed = {
                **state,
                "chunks": sorted(
                    [chunk for chunk in state["chunks"] if chunk["year"] != body["year"]]
                    + [descriptor],
                    key=lambda chunk: chunk["year"],
                ),
            }
            try:
                history = self._history(proposed)
            except HistoryContinuityError:
                self._quarantine(entity, "history-import-chunk", reference)
                raise
        reference = version(proposed)
        with self.store.documents.transaction():
            self.store.archive("history-import", reference, proposed)
            self.store.documents.write(
                "history-import-current-" + self._cohort_key(entity) + ".json", reference
            )
            if self.issues.get(entity) == "history_continuity_unavailable":
                self.store.documents.write(
                    "history-import-quarantine-" + self._cohort_key(entity) + ".json", None
                )
        self.issues.pop(entity, None)
        self.states[entity] = proposed
        return history
