"""Freeze one pre-midnight forecast context per HA calendar day in the shared database."""

import logging
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.forecast_quality.bindings import bindings
from open_energy_planner.forecast_quality.policy import RECORDER_DELAY_MINUTES
from open_energy_planner.home_assistant.names import source_names

DAILY_CAPTURE_POLL_SECONDS = 5
MAX_CAPTURE_GAP_SECONDS = DAILY_CAPTURE_POLL_SECONDS * 2
DAY_PAGE_SIZE = 20
INDEX_NAME = "quality-days.json"
_LOGGER = logging.getLogger(__name__)


def calendar_day(day):
    """Reject non-canonical ISO date forms before using dates as archive or pagination keys."""
    calendar_day = date.fromisoformat(day)
    if calendar_day.isoformat() != day:
        raise ValueError("Calendar day must use YYYY-MM-DD")
    return calendar_day


def day_window(day, timezone):
    """Return actual UTC bounds of a local calendar day, including 23/25-hour DST days."""
    local_day = calendar_day(day)
    zone = ZoneInfo(timezone)
    start = datetime.combine(local_day, time(), zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time(), zone).astimezone(UTC)
    if end <= start or start.astimezone(zone).date() != local_day:
        raise ValueError("Calendar day does not exist in the installation timezone")
    return start, end


def forecast_context(demand, solar, catalogue, original):
    """Capture only original forecast references and source identities, independently of solver success."""
    sources = bindings(
        demand.configuration, solar.configuration, demand.consumers.selected, catalogue
    )
    entities = {row["entity_id"] for row in sources}
    context = {
        "forecast_quality_sources": sources,
        "forecast_quality_names": {
            entity: name for entity, name in source_names(catalogue).items() if entity in entities
        },
        "display_sources": {
            source.source_id: source.entity_id
            for source in (solar.configuration.sources if solar.configuration else ())
        },
        "forecast_quality_controlled": (original or {}).get("forecast_quality_controlled", []),
        "comparison_names": dict((original or {}).get("comparison_names", {})),
        "input_snapshot": {"demand_evidence": [], "solar_evidence": []},
    }
    context["expected_sources"] = (
        [{"id": "household", "title": "Household consumption"}]
        if demand.configuration is not None
        else []
    )
    context["expected_sources"].extend(
        {
            "id": row["entity_id"],
            "title": context["forecast_quality_names"].get(row["entity_id"], row["entity_id"]),
        }
        for row in demand.consumers.selected
    )
    for source in solar.configuration.sources if solar.configuration else ():
        title = context["forecast_quality_names"].get(source.entity_id, source.source_id)
        context["comparison_names"][source.source_id] = title
        context["expected_sources"].append({"id": source.source_id, "title": title})
    for family, forecast, configuration in (
        ("demand", demand.forecast, demand.configuration),
        ("solar", solar.forecast, solar.configuration),
    ):
        if forecast is None or configuration is None:
            continue
        if forecast.configuration_version != configuration.version:
            continue
        entry = {
            "forecast_version": forecast.version,
            "configuration_version": forecast.configuration_version,
            "model": forecast.model,
            "issued_at": forecast.issued_at.isoformat(),
        }
        if family == "solar":
            entry["source_ids"] = [source.source_id for source in configuration.sources]
        context["input_snapshot"][family + "_evidence"].append(entry)
    return deepcopy(context)


class DailyForecastArchive:
    """Own immutable midnight baselines independently of plan publication.

    One capture task calls observe in a worker thread outside the app state lock.
    It exclusively owns previous and pending. Concurrent API reads use committed
    documents. Baselines use the last pre-midnight observation. Startup during a
    day cannot reconstruct an unrecorded midnight. Baseline and index commit together.
    """

    def __init__(self, documents):
        """Borrow the active demand namespace without restoring an unobserved midnight across restart."""
        self.documents = documents
        self.previous = None
        self.pending = None

    def _persist(self):
        """Retry the same captured baseline after a transient failure, without replacing its original context."""
        if self.pending is None:
            return
        record = self.pending
        name = "quality-day-" + record["day"] + ".json"
        created = False
        with self.documents.transaction():
            if not self.documents.exists(name):
                self.documents.write(name, record)
                index = self.documents.read(INDEX_NAME) if self.documents.exists(INDEX_NAME) else []
                self.documents.write(INDEX_NAME, sorted(set(index) | {record["day"]}, reverse=True))
                created = True
        if created:
            _LOGGER.info(
                "Daily forecast evidence archived for %s. Midnight capture: %s.",
                record["day"],
                "complete"
                if not record.get("capture_issue")
                else "incomplete. Review forecast quality coverage",
            )
        self.pending = None

    def observe(self, now, timezone, context):
        """Freeze on a closely observed calendar transition, never from a later intraday forecast."""
        self._persist()
        zone = ZoneInfo(timezone)
        day = now.astimezone(zone).date().isoformat()
        previous = self.previous
        if previous is not None and now < previous["at"]:
            return
        if previous is not None and previous["timezone"] == timezone:
            previous_day = previous["at"].astimezone(zone).date().isoformat()
            if previous_day != day:
                start, end = day_window(day, timezone)
                complete_capture = (
                    previous["at"] < start <= now
                    and (now - previous["at"]).total_seconds() <= MAX_CAPTURE_GAP_SECONDS
                )
                frozen = deepcopy(previous["context"]) if complete_capture else None
                if frozen is not None:
                    for entries in frozen["input_snapshot"].values():
                        entries[:] = [
                            entry
                            for entry in entries
                            if datetime.fromisoformat(entry["issued_at"]) <= start
                        ]
                record = {
                    "day": day,
                    "timezone": timezone,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "captured_at": now.isoformat(),
                    "observed_at": previous["at"].isoformat(),
                    "context": frozen,
                    "capture_issue": None
                    if complete_capture
                    else "Midnight was not continuously observed. No daily forecast was recorded",
                }
                record["version"] = digest(record)
                self.pending = record
                self._persist()
        self.previous = {"at": now, "timezone": timezone, "context": deepcopy(context)}

    def read(self, day, timezone):
        """Return the original baseline or an explicit absence, without writing historical forecasts."""
        start, end = day_window(day, timezone)
        name = "quality-day-" + day + ".json"
        if self.documents.exists(name):
            return self.documents.read(name)
        return {
            "day": day,
            "timezone": timezone,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "captured_at": None,
            "context": None,
            "capture_issue": "No midnight forecast was recorded for this day",
        }

    def page(self, now, timezone, before=None):
        """List retained days newest first, including today's honest pending or missing baseline."""
        today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
        index = self.documents.read(INDEX_NAME) if self.documents.exists(INDEX_NAME) else []
        days = sorted(set(index) | {today}, reverse=True)
        if before is not None:
            calendar_day(before)
            days = [day for day in days if day < before]
        selected = days[:DAY_PAGE_SIZE]
        return {
            "days": selected,
            "next_before": selected[-1] if len(days) > len(selected) else None,
            "timezone": timezone,
        }


def controlled_consumers(plans, start, end):
    """Reuse atomic publication history to exclude consumers controlled during the day.

    Publication timestamps, rather than calculation start times, assign controls
    across midnight and restarts. Read failures propagate instead of granting a
    score. Include the plan already active at the day boundary conservatively.
    """
    from ..savings.storage.adapters.sqlalchemy import SettlementStore

    return plans.controlled_entities(
        activation["plan_id"]
        for activation in SettlementStore(plans.engine).activations(start, end)
        if activation["plan_id"] is not None and activation["enabled"]
    )


def daily_evidence(
    record, archive, demand_documents, solar_documents, now, controlled=(), *, names=None
):
    """Score a frozen whole day only after all measurements arrive. Partial totals never imply a daily score."""
    from .evidence import evidence

    start, end = (datetime.fromisoformat(record[key]) for key in ("start", "end"))
    context = deepcopy(record.get("context"))
    result = {
        **{
            key: record.get(key)
            for key in ("day", "timezone", "start", "end", "captured_at", "version")
        },
        "as_of": now.isoformat(),
        "series": [],
        "issues": [],
    }
    if context is None:
        result["issues"].append(record["capture_issue"])
        return result
    context["forecast_quality_controlled"] = sorted(
        set(context.get("forecast_quality_controlled", [])) | set(controlled)
    )
    original = evidence(
        context, archive, demand_documents, solar_documents, now, window=(start, end), names=names
    )
    result.update(series=original["series"], issues=original["issues"])
    resolved = {source["id"] for source in result["series"]}
    result["issues"].extend(
        "Midnight forecast unavailable for " + source["title"]
        for source in context.get("expected_sources", [])
        if source["id"] not in resolved
    )
    boundary_issue = (
        "Hourly Recorder evidence cannot exactly resolve this local calendar-day boundary"
        if start.minute or end.minute
        else None
    )
    if boundary_issue:
        result["issues"].append(boundary_issue)
    if not result["series"]:
        result["issues"].append("No usable energy forecasts were available at midnight")
    completed = now >= end + timedelta(minutes=RECORDER_DELAY_MINUTES)
    for source in [
        candidate
        for original in result["series"]
        for candidate in (original, *original.get("alternatives", ()))
    ]:
        rows = source["rows"]
        predicted = (
            fsum(row["predicted_kwh"] for row in rows)
            if rows and all(row["predicted_kwh"] is not None for row in rows)
            else None
        )
        actual = (
            fsum(row["measured_kwh"] for row in rows)
            if rows and all(row["measured_kwh"] is not None for row in rows)
            else None
        )
        eligible = completed and bool(rows) and all(row["error_kwh"] is not None for row in rows)
        issue = None
        if not completed:
            issue = "Daily evaluation waits for the completed day and Recorder delay"
        elif not eligible:
            issue = (
                boundary_issue
                or source["comparison_issue"]
                or next(
                    (row["evaluation_issue"] for row in rows if row["evaluation_issue"]),
                    "Complete comparable daily evidence unavailable",
                )
            )
        error = predicted - actual if eligible else None
        source["daily"] = {
            "predicted_kwh": predicted,
            "measured_kwh": actual,
            "error_kwh": error,
            "absolute_error_kwh": abs(error) if error is not None else None,
            "relative_error_percent": abs(error) / actual * 100
            if error is not None and actual > 0
            else None,
            "evaluation_issue": issue,
        }
        if not eligible:
            source["metrics"] = dict.fromkeys(source["metrics"])
            for row in rows:
                row["error_kwh"] = None
                row["evaluation_issue"] = row["evaluation_issue"] or issue
    return result
