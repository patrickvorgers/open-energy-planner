"""Resume bounded energy-history transfers while retaining all available measurement years."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite

from open_energy_planner.history.model import MAX_CUMULATIVE_ENERGY_KWH, MAX_HISTORY_HOURS
from open_energy_planner.history.storage import history_version as version
from open_energy_planner.prices.model import aware

HISTORY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
MAX_REQUEST_DAYS = 367
DISCOVERY_BASIS = "energy_history_range"
CHUNK_BASIS = "energy_history_chunk"
ALIGNMENT_POLICY = "nearest_utc_hour_64s"
MAX_ALIGNMENT_SECONDS = 64


@dataclass(frozen=True)
class EnergyEndpoint:
    """One cumulative Recorder sum endpoint. Physical state is retained only as source evidence."""

    start: datetime
    total_kwh: float


@dataclass(frozen=True)
class CumulativeHistory:
    """Energy-only ordered endpoints, without financial physical-meter baseline semantics."""

    entity_id: str
    hours: tuple[EnergyEndpoint, ...]


class HistoryContinuityError(ValueError):
    """Prevent a changed Recorder statistics identity from replacing the accepted source cohort."""


@dataclass(frozen=True)
class HistoryReport:
    """One validated availability envelope or bounded hourly transfer from Recorder."""

    basis: str
    entity_id: str
    statistics_identity: dict
    history: CumulativeHistory | None = None
    available_start: datetime | None = None
    raw_hours: tuple = ()
    excluded_starts: frozenset[datetime] = frozenset()


def _finite(value):
    """Keep finite numeric source evidence without treating booleans as energy or timestamps."""
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _normalized_hours(rows):
    """Recover bounded timestamp rounding and exclude ambiguous sums independently of meter state."""
    if not isinstance(rows, list) or len(rows) > MAX_HISTORY_HOURS:
        raise ValueError("Invalid energy history size")
    raw, hours, excluded = [], {}, set()
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            raise ValueError("Invalid energy history row")
        stamp, total, meter = (value if _finite(value) else None for value in row)
        raw.append((stamp, total, meter))
        if stamp is None or total is None or abs(total) > MAX_CUMULATIVE_ENERGY_KWH:
            continue
        canonical = (stamp + 1800) // 3600 * 3600
        if abs(stamp - canonical) > MAX_ALIGNMENT_SECONDS:
            continue
        try:
            instant = datetime.fromtimestamp(canonical, UTC)
        except (OverflowError, OSError, ValueError):
            continue
        if instant < HISTORY_EPOCH:
            continue
        hour = EnergyEndpoint(instant, total)
        if instant in hours and hours[instant] != hour:
            excluded.add(instant)
        hours[instant] = hour
    return (
        tuple(hours[instant] for instant in sorted(hours) if instant not in excluded),
        tuple(raw),
        frozenset(excluded),
    )


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
        raise ValueError("Invalid energy statistics identity")
    return dict(value)


def parse_report(body, *, discovery_basis=DISCOVERY_BASIS, chunk_basis=CHUNK_BASIS):
    """Parse energy-only availability or hourly responses without expanding shared wire limits."""
    if not isinstance(body, dict):
        raise TypeError("Invalid energy history report")
    common = {"request_version", "basis", "entity_id", "statistics_identity"}
    basis = body.get("basis")
    expected = common | ({"available_start"} if basis == discovery_basis else {"unit", "hours"})
    if basis not in (discovery_basis, chunk_basis) or set(body) != expected:
        raise ValueError("Invalid energy history fields")
    if (
        not isinstance(body["request_version"], str)
        or not isinstance(body["entity_id"], str)
        or not body["entity_id"].startswith("sensor.")
    ):
        raise ValueError("Invalid energy history source")
    identity = _statistics_identity(body["statistics_identity"])
    if basis == discovery_basis:
        first = body["available_start"]
        if first is not None:
            if not isinstance(first, str):
                raise ValueError("Invalid energy availability start")
            first = aware(datetime.fromisoformat(first))
            if first < HISTORY_EPOCH or first.minute or first.second or first.microsecond:
                raise ValueError("Invalid energy availability boundary")
        return HistoryReport(basis, body["entity_id"], identity, available_start=first)
    if body["unit"] != "kWh":
        raise ValueError("Unsupported energy history unit")
    hours, raw, excluded = _normalized_hours(body["hours"])
    history = CumulativeHistory(body["entity_id"], hours) if hours else None
    return HistoryReport(
        basis, body["entity_id"], identity, history=history, raw_hours=raw, excluded_starts=excluded
    )


class HistoryImport:
    """Keep immutable annual chunks and a resumable source-bound current import pointer.

    Annual Recorder transfers remain bounded. The in-memory aggregate has no
    functional year limit. Empty years retain explicit completed coverage and
    cannot truncate later years. Revised sums exclude only their affected
    endpoints. Changed source identity retains the original blocked cohort.
    """

    def __init__(self, store, *, discovery_basis=DISCOVERY_BASIS, chunk_basis=CHUNK_BASIS):
        """Borrow the existing energy database namespace without adding tables or weather ownership."""
        self.store = store
        self.discovery_basis = discovery_basis
        self.chunk_basis = chunk_basis
        self.states = {}
        self.issues = {}
        self.learning_starts = {}
        self._chunks = {}

    @contextmanager
    def transaction(self):
        """Commit source and accepted-cohort pointers together, retaining continuity faults on rejection."""
        states, issues, chunks = self.states.copy(), self.issues.copy(), self._chunks.copy()
        continuity_error = None
        try:
            with self.store.transaction():
                try:
                    yield
                except HistoryContinuityError as error:
                    continuity_error = error
        except BaseException:
            self.states, self.issues, self._chunks = states, issues, chunks
            raise
        if continuity_error is not None:
            raise continuity_error

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
        self._chunks = {key: value for key, value in self._chunks.items() if key[0] in starts}

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
        with self.store.transaction():
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
            raise ValueError("Invalid energy history quarantine")
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
            raise ValueError("Invalid energy history quarantine")
        self.issues[entity] = fault["issue"]

    def _state(self, entity, reference=None):
        """Load and validate the source's exact immutable import state once per runtime."""
        current = reference is None
        if current and entity in self.states:
            return self.states[entity]
        pointer = "history-import-current-" + self._cohort_key(entity) + ".json"
        state = None
        if not current or self.store.documents.exists(pointer):
            if current:
                reference = self.store.documents.read(pointer)
            if (
                not isinstance(reference, str)
                or len(reference) != 64
                or any(character not in "0123456789abcdef" for character in reference)
            ):
                raise ValueError("Invalid energy history pointer")
            state = self.store.documents.read("history-import-" + reference + ".json")
            if (
                not isinstance(state, dict)
                or set(state) - {"conflicts"}
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
                raise ValueError("Invalid energy history import")
            _statistics_identity(state["statistics_identity"])
            conflicts = state.get("conflicts", [])
            if not isinstance(conflicts, list):
                raise ValueError("Invalid energy history conflicts")
            for row in conflicts:
                if (
                    not isinstance(row, list)
                    or len(row) != 2
                    or not _finite(row[0])
                    or row[0] % 3600
                    or not _finite(row[1])
                    or abs(row[1]) > MAX_CUMULATIVE_ENERGY_KWH
                ):
                    raise ValueError("Invalid energy history conflict")
                try:
                    if datetime.fromtimestamp(row[0], UTC) < HISTORY_EPOCH:
                        raise ValueError("Invalid energy history conflict")
                except (OverflowError, OSError):
                    raise ValueError("Invalid energy history conflict") from None
            if len({row[0] for row in conflicts}) != len(conflicts):
                raise ValueError("Duplicate energy history conflict")
            aware(datetime.fromisoformat(state["discovered_at"]))
            if state["available_start"] is not None:
                first = aware(datetime.fromisoformat(state["available_start"]))
                if first < HISTORY_EPOCH:
                    raise ValueError("Invalid energy history discovery")
            years = [chunk["year"] for chunk in state["chunks"]]
            if len(set(years)) != len(years) or any(
                type(year) is not int or year < 1970 for year in years
            ):
                raise ValueError("Invalid energy history years")
        if current:
            self.states[entity] = state
        return state

    def checkpoint(self, entities):
        """Identify the accepted immutable source states for an atomic multi-meter cohort."""
        return {entity: version(self._state(entity)) for entity in entities}

    def restore_checkpoint(self, references):
        """Restore an accepted cohort independently of newer partially imported source pointers."""
        if not isinstance(references, dict):
            raise TypeError("Invalid energy history cohort")
        histories = {}
        for entity, reference in references.items():
            if not isinstance(reference, str) or len(reference) != 64:
                raise ValueError("Invalid energy history cohort reference")
            state = self._state(entity, reference=reference)
            history = self._history(state)
            if history is not None:
                histories[entity] = history
        return histories

    def _chunk(self, state, descriptor):
        """Verify a bounded immutable chunk before admitting any cached measurement."""
        key = state["entity_id"], descriptor["year"]
        binding = (
            descriptor["reference"],
            descriptor["start"],
            descriptor["end"],
            version(state["statistics_identity"]),
            state.get("learning_start"),
        )
        cached = self._chunks.get(key)
        if cached is not None and cached[0] == binding:
            return cached[1]
        body = self.store.documents.read(
            "history-import-chunk-" + descriptor["reference"] + ".json"
        )
        if (
            not isinstance(body, dict)
            or set(body) - {"alignment"}
            != {"entity_id", "statistics_identity", "year", "start", "end", "hours"}
            or body.get("alignment", ALIGNMENT_POLICY) != ALIGNMENT_POLICY
            or version(body) != descriptor["reference"]
            or body["entity_id"] != state["entity_id"]
            or body["statistics_identity"] != state["statistics_identity"]
            or any(body[field] != descriptor[field] for field in ("year", "start", "end"))
        ):
            raise ValueError("Invalid cached energy history chunk")
        start, end = (aware(datetime.fromisoformat(body[field])) for field in ("start", "end"))
        if not timedelta(0) < end - start <= timedelta(days=MAX_REQUEST_DAYS):
            raise ValueError("Invalid cached energy history range")
        report = parse_report(
            {
                "request_version": "cached",
                "basis": self.chunk_basis,
                "entity_id": body["entity_id"],
                "statistics_identity": body["statistics_identity"],
                "unit": "kWh",
                "hours": body["hours"],
            },
            discovery_basis=self.discovery_basis,
            chunk_basis=self.chunk_basis,
        )
        hours = (
            tuple(hour for hour in report.history.hours if start <= hour.start < end)
            if report.history
            else ()
        )
        admitted = replace(
            report,
            history=CumulativeHistory(state["entity_id"], hours) if hours else None,
            excluded_starts=frozenset(
                instant for instant in report.excluded_starts if start <= instant < end
            ),
        )
        self._chunks[key] = binding, admitted
        return admitted

    def _history(self, state):
        """Merge matching overlapping endpoints while preserving every absent hour as a gap."""
        rows = {}
        excluded = {datetime.fromtimestamp(row[0], UTC) for row in state.get("conflicts", [])}
        for descriptor in state["chunks"]:
            report = self._chunk(state, descriptor)
            excluded.update(report.excluded_starts)
            for hour in report.history.hours if report.history else ():
                if hour.start in rows and rows[hour.start] != hour:
                    excluded.add(hour.start)
                rows[hour.start] = hour
        hours = tuple(rows[key] for key in sorted(rows) if key not in excluded)
        return CumulativeHistory(state["entity_id"], hours) if hours else None

    def restore(self, entities):
        """Restore complete accepted chunks for selected meter identities and isolate cache faults."""
        entities = tuple(entities)
        self._chunks = {key: value for key, value in self._chunks.items() if key[0] not in entities}
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
                "basis": self.discovery_basis,
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
                "basis": self.chunk_basis,
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
            not isinstance(report, HistoryReport)
            or report.entity_id != entity
            or report.basis != request["basis"]
        ):
            raise ValueError("Energy history source/range mismatch")
        identity = _statistics_identity(report.statistics_identity)
        start, end = (aware(datetime.fromisoformat(request[field])) for field in ("start", "end"))
        state = self._state(entity)
        if request["basis"] == self.discovery_basis:
            if report.available_start is not None and not start <= report.available_start < end:
                raise ValueError("Energy availability outside requested range")
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
                raise HistoryContinuityError("Energy statistics identity changed")
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
                "conflicts": state.get("conflicts", []) if state else [],
            }
            history = self._history(proposed)
        else:
            if state is None:
                raise ValueError("Energy history source/range mismatch")
            body = {
                "entity_id": entity,
                "statistics_identity": identity,
                "year": request["import_year"],
                "start": request["start"],
                "end": request["end"],
                "hours": [list(row) for row in report.raw_hours],
                "alignment": ALIGNMENT_POLICY,
            }
            reference = version(body)
            self.store.archive("history-import-chunk", reference, body)
            if identity != state["statistics_identity"]:
                self._quarantine(entity, "history-import-chunk", reference)
                raise HistoryContinuityError("Energy statistics identity changed")
            previous = next(
                (chunk for chunk in state["chunks"] if chunk["year"] == body["year"]), None
            )
            fresh = (
                {hour.start.timestamp(): hour.total_kwh for hour in report.history.hours}
                if report.history
                else {}
            )
            conflicts = dict(state.get("conflicts", []))
            if previous:
                old = self._chunk(state, previous).history
                for hour in old.hours if old else ():
                    stamp = hour.start.timestamp()
                    if start <= hour.start < end and fresh.get(stamp) != hour.total_kwh:
                        conflicts.setdefault(stamp, hour.total_kwh)
            conflicts = {
                stamp: total
                for stamp, total in conflicts.items()
                if not start.timestamp() <= stamp < end.timestamp() or fresh.get(stamp) != total
            }
            descriptor = {field: body[field] for field in ("year", "start", "end")}
            descriptor["reference"] = reference
            proposed = {
                **state,
                "conflicts": [[stamp, total] for stamp, total in sorted(conflicts.items())],
                "chunks": sorted(
                    [chunk for chunk in state["chunks"] if chunk["year"] != body["year"]]
                    + [descriptor],
                    key=lambda chunk: chunk["year"],
                ),
            }
            history = self._history(proposed)
        reference = version(proposed)
        with self.store.transaction():
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
