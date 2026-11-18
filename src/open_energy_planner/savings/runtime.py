"""App-owned settlement orchestration. Original plans and measured evidence are never rewritten."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from zoneinfo import ZoneInfo

from open_energy_planner.core.calendar import day_window
from open_energy_planner.installation.location import SiteLocation

from ..contracts.bonus import CounterSeed
from .bonus import HistoricalBonuses
from .model import (
    MAX_BATCH_INTERVALS,
    MISSING_MEASUREMENTS,
    SETTLEMENT_INTERVAL,
    PendingSettlement,
    parse_frame,
)

SETTLEMENT_RECORDER_DELAY = timedelta(minutes=10)
MAX_RECOVERY_DAYS_PER_READ = 4
MAX_EVALUATION_VERSIONS = 20
SHA256_HEX_LENGTH = 64

# Only fixed owned guidance is public. Other caught exceptions may contain stored or driver values.
PENDING_SETTLEMENT_MESSAGES = {
    "Without-battery comparison requires measured AC solar energy": "The solar source measures DC energy. A without-battery cash comparison requires measured AC solar energy at the household boundary.",
    "Without-battery bonus usage is unknown after missing measurements": "The export allowance without a battery cannot be reconstructed from earlier missing evidence. No actual counter was substituted.",
    "A reset inside the bonus anchor interval prevents exact reconstruction": "An export meter reset prevents reconstruction of the bonus starting total.",
    "Confirmed bonus meter disagrees with its historical boundary": "The confirmed export reading disagrees with the reading in history.",
    "Confirmed bonus meter is outside its historical interval": "The confirmed export reading falls outside the historical interval's readings.",
    "Historical bonus anchor does not match the export meters or period": "The bonus starting reading belongs to different export meters or a different period.",
    "Historical bonus baseline awaits captured export evidence": "The stored export measurements cannot yet connect the historical interval to the bonus checkpoint.",
    "Historical bonus interval predates the confirmed statistics reference": "The requested interval starts before the confirmed bonus statistics reference.",
    "Historical bonus history unavailable: history_missing": "The requested historical bonus statistics endpoints are missing from Home Assistant.",
    "Historical bonus baseline awaits completed hourly statistics": "The hourly export statistics needed for the bonus starting total are not complete yet.",
    "Historical bonus baseline is being read from Home Assistant": "The bonus starting total is still being read from Home Assistant.",
    "Historical bonus counter archive is unreadable": "The stored bonus starting totals cannot currently be read.",
    "Historical bonus export evidence has a gap or changed meters": "Historical export readings have a gap or use different export meters.",
    "Historical bonus history is incomplete or invalid": "The received bonus export history is incomplete or invalid.",
    "Historical bonus history unavailable: history_unavailable": "Historical export measurements are unavailable from Home Assistant.",
    "Historical bonus history unavailable: recorder_unavailable": "Home Assistant Recorder is unavailable for the historical export read.",
    "Historical bonus history unavailable: unsupported_statistics": "Home Assistant cannot supply supported statistics for these export meters.",
    "Historical bonus period does not start on a whole UTC hour. Confirm a manual baseline": "The bonus period starts between hourly readings and needs a confirmed manual starting total.",
    "Historical exports exceed the confirmed used bonus volume": "Historical exports exceed the confirmed export total used for the bonus.",
    "Published price coverage is missing": "Published electricity prices are missing for this measurement interval.",
    "Validated physical bonus endpoint is unavailable": "The confirmed export measurement needed to continue the bonus calculation is unavailable.",
}
SETTLEMENT_VALIDATION_MESSAGES = ("Contract coverage is missing",)


def settlement_issue(error):
    """Project safe evidence guidance without copying retained values or arbitrary exception text."""
    messages = (
        PENDING_SETTLEMENT_MESSAGES
        if isinstance(error, PendingSettlement)
        else SETTLEMENT_VALIDATION_MESSAGES
    )
    for message in messages:
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] == message:
            return message
    if isinstance(error, PendingSettlement):
        return (
            "Settlement is waiting for complete validated evidence. "
            "Existing successful results are retained"
        )
    return (
        "Retained settlement evidence is invalid. Check recorded meter data and published prices. "
        "Existing successful results are retained"
    )


def version(value):
    """Hash finite detached settlement inputs without connection or provider details."""
    return sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def quarter(instant):
    """Floor an aware instant to a settlement boundary in UTC."""
    instant = instant.astimezone(UTC)
    seconds = int(SETTLEMENT_INTERVAL.total_seconds())
    return datetime.fromtimestamp(int(instant.timestamp()) // seconds * seconds, UTC)


def capture_start(instant):
    """Start at the first complete quarter after publication, preserving an exactly aligned midnight."""
    boundary = quarter(instant)
    return boundary if instant == boundary else boundary + SETTLEMENT_INTERVAL


class SavingsRuntime:
    """Keep measured-energy ingestion and immutable historical readers outside planning."""

    def __init__(self, store, documents, plans):
        """Borrow app-owned stores. A dedicated settlement lock serializes mutations."""
        self.store, self.documents, self.plans = store, documents, plans
        self.bonus = HistoricalBonuses(store)
        self.issue = None
        self.attention = False
        self.events = None
        self._request_bindings = None, None, None
        self._recovery_search = None, None

    def request(self, now, settings, solar, demand, seeds, location):
        """Request at most one day after first publication. Wait for completed Recorder statistics."""
        if settings is None or solar is None or demand is None:
            return None
        first = self.store.first_activation()
        if first is None:
            return None
        start = capture_start(datetime.fromisoformat(first))
        if self.documents.exists("capture.json"):
            start = datetime.fromisoformat(self.documents.read("capture.json")["end"])
        end = min(
            quarter(now - SETTLEMENT_RECORDER_DELAY),
            start + SETTLEMENT_INTERVAL * MAX_BATCH_INTERVALS,
        )
        if start >= end:
            recovery = self._missing_capture(now, location)
            if recovery is None:
                return None
            start, end = recovery
        bindings = {
            "terms": [asdict(energy_term) for energy_term in demand.terms],
            "solar": [asdict(source) for source in solar.sources],
            "batteries": [asdict(battery) for battery in settings.batteries],
        }
        plan_id = self.store.context_plan(start)
        market_identity = None
        if plan_id:
            cached_plan, historical_bindings, market_identity = self._request_bindings
            if cached_plan != plan_id:
                original = self.plans.get_context(plan_id)
                historical_bindings = original.get("comparison_bindings") if original else None
                market_identity = (
                    {key: original[key] for key in ("currency", "market_area") if key in original}
                    if original
                    else None
                )
                self._request_bindings = plan_id, historical_bindings, market_identity
            bindings = historical_bindings or bindings
        changed = self.store.next_configuration(start, end, bindings)
        if changed:
            end = datetime.fromisoformat(changed)
        request = {"start": start.isoformat(), "end": end.isoformat(), "bindings": bindings}
        if market_identity:
            request["market_identity"] = market_identity
        request["version"] = version(request)
        return request

    def _missing_capture(self, now, location):
        """Rotate bounded recovery reads after acknowledged attempts, including empty replies.

        Daily reports own the recovery window and distinguish absent metering from
        accounting failures. Retained frames remain immutable. A retry fills only
        missing intervals and never moves the forward capture cursor backwards.
        The independent durable recovery cursor prevents a permanent first gap
        from starving later gaps. A read-only cache pages past days without gaps.
        It does not advance a selected attempt before its receipt commits.
        """
        if location is None:
            return None
        today = now.astimezone(ZoneInfo(location.timezone)).date()
        last, _ = day_window(today.isoformat(), location.timezone)
        initial = capture_start(datetime.fromisoformat(self.store.first_activation()))
        checkpoint = (
            self.documents.read("recovery.json")["end"]
            if self.documents.exists("recovery.json")
            else initial.isoformat()
        )
        cached_checkpoint, cached_cursor = self._recovery_search
        cursor = (
            cached_cursor if cached_checkpoint == checkpoint else datetime.fromisoformat(checkpoint)
        )
        wrapped = False
        for _ in range(MAX_RECOVERY_DAYS_PER_READ):
            first, _ = day_window(
                cursor.astimezone(ZoneInfo(location.timezone)).date().isoformat(), location.timezone
            )
            report = self.store.day(after=first, before=last, finalized=False)
            if report is None:
                if wrapped:
                    return None
                cursor, wrapped = initial, True
                first, _ = day_window(
                    initial.astimezone(ZoneInfo(location.timezone)).date().isoformat(),
                    location.timezone,
                )
                report = self.store.day(after=first, before=last, finalized=False)
                if report is None:
                    return None
            for gap in report["scenarios"]["actual"]["missing"]:
                if gap["reason"] == MISSING_MEASUREMENTS:
                    start, end = (datetime.fromisoformat(gap[key]) for key in ("start", "end"))
                    start = max(start, cursor)
                    end = min(
                        end,
                        quarter(now - SETTLEMENT_RECORDER_DELAY),
                        start + SETTLEMENT_INTERVAL * MAX_BATCH_INTERVALS,
                    )
                    if start >= end:
                        continue
                    while start < end:
                        retained = self.store.frames(start, end=end)
                        if not retained:
                            return start, end
                        for row in retained:
                            frame = parse_frame(row["frame"])
                            if frame.start > start:
                                return start, frame.start
                            start = max(start, frame.end)
            cursor = datetime.fromisoformat(report["end"])
            self._recovery_search = checkpoint, cursor
        return None

    def receive(
        self,
        pending: dict,
        body: object,
        now: datetime,
        seeds: tuple[CounterSeed, ...],
        location: SiteLocation | None,
    ) -> None:
        """Validate scanned coverage and measured intervals before atomically archiving them.

        Each frame spans at most SETTLEMENT_INTERVAL (a timedelta representing
        fifteen minutes). With scanned_until, absent intervals remain unmeasured
        while later frames advance capture. Legacy reports require a contiguous
        prefix. Only request edges may be off-grid after a configuration change.
        Interior boundaries align to UTC quarters. A scan may stop at a complete
        quarter before the requested end. Recovery fills absent evidence without
        rewinding the cursor or replacing retained frames. Frame documents kWh
        units, sign rules and reconstruction quality.

        Args:
            pending: App-owned versioned request with aware bounds and frozen
                Energy bindings. It is not supplied by the reporting client.
            body: Untrusted JSON response with request identity, frames and
                disjoint import/export meter identities.
            now: Aware current time. Future measurement intervals are rejected.
            seeds: Confirmed opening bonus positions retained with the evidence.
            location: Optional installation coordinates/timezone for later billing.

        Returns:
            None after immutable frames and capture.json have committed in one
            transaction. Capture acceptance does not resolve an outstanding
            financial or prerequisite issue. Its owner confirms recovery.

        Raises:
            ValueError: Request identity, interval coverage, contiguity, quarter
                boundaries, meter roles, channels or finite energy values are
                invalid, or stored immutable evidence conflicts with this batch.
            TypeError: Timestamp fields have unsupported JSON types.

        All intake validation finishes before any write. Storage conflicts or
        other transaction failures leave no durable partial frames or pointer
        advance. The caller serializes ingestion with other settlement mutations.
        """
        if not isinstance(body, dict) or body.get("request_version") != pending["version"]:
            raise ValueError("Settlement request changed")
        sparse = "scanned_until" in body
        if set(body) != {"request_version", "frames", "imports", "exports"} | (
            {"scanned_until"} if sparse else set()
        ):
            raise ValueError("Invalid settlement response")
        frames = body["frames"]
        if (
            not isinstance(frames, list)
            or not (0 if sparse else 1) <= len(frames) <= MAX_BATCH_INTERVALS
        ):
            raise ValueError("Invalid settlement batch")
        frames = tuple(parse_frame(row) for row in frames)
        start, end = (
            datetime.fromisoformat(pending["start"]),
            datetime.fromisoformat(pending["end"]),
        )
        scanned = datetime.fromisoformat(body["scanned_until"]) if sparse else frames[-1].end
        if (
            scanned.utcoffset() is None
            or not start < scanned <= min(end, now)
            or any(frame.start < start or frame.end > scanned for frame in frames)
            or (not sparse and frames[0].start != start)
        ):
            raise ValueError("Settlement response outside requested coverage")
        if any(
            previous_item.end > next_item.start
            or (not sparse and previous_item.end != next_item.start)
            for previous_item, next_item in pairwise(frames)
        ):
            raise ValueError("Settlement batch has gaps or overlaps")
        # Only request edges may split a quarter when physical configuration changes.
        if (scanned != end and scanned != quarter(scanned)) or any(
            (frame.start != start and frame.start != quarter(frame.start))
            or (frame.end != end and frame.end != quarter(frame.end))
            for frame in frames
        ):
            raise ValueError("Settlement frame does not end on a requested quarter boundary")
        terms = {
            energy_term["entity_id"]: energy_term["sign"]
            for energy_term in pending["bindings"]["terms"]
        }
        for kind, sign in (("imports", 1), ("exports", -1)):
            ids = body[kind]
            if (
                not isinstance(ids, list)
                or len(ids) != len(set(ids))
                or any(terms.get(device_id) != sign for device_id in ids)
                or (kind in pending["bindings"] and set(ids) != set(pending["bindings"][kind]))
            ):
                raise ValueError("Grid settlement meters do not match Energy configuration")
        if not body["imports"]:
            raise ValueError("Grid import measurement is required")
        allowed = (
            set(terms)
            | {solar_flow["entity_id"] for solar_flow in pending["bindings"]["solar"]}
            | {battery["energy_entity"] for battery in pending["bindings"]["batteries"]}
        )
        batteries = {battery["device_id"] for battery in pending["bindings"]["batteries"]}
        for frame in frames:
            if (
                set(frame.channels) != allowed
                or set(frame.meter_start) - allowed
                or not set(frame.battery_start) <= batteries
                or not set(frame.battery_end) <= batteries
            ):
                raise ValueError("Settlement channels do not match requested Energy bindings")
        context = json.loads(
            json.dumps(
                {
                    "bindings": {
                        **pending["bindings"],
                        "imports": body["imports"],
                        "exports": body["exports"],
                    },
                    **(
                        {"market_identity": pending["market_identity"]}
                        if pending.get("market_identity")
                        else {}
                    ),
                    "seeds": [asdict(seed_item) for seed_item in seeds],
                    "location": asdict(location) if location else None,
                },
                default=lambda value_item: value_item.isoformat(),
            )
        )
        # Raw capture and pointer share the same database transaction.
        with self.documents.transaction():
            self.store.put_frames(frames, context, self.documents.pending.connection)
            previous = (
                datetime.fromisoformat(self.documents.read("capture.json")["end"])
                if self.documents.exists("capture.json")
                else start
            )
            self.documents.write("capture.json", {"end": max(previous, scanned).isoformat()})
            if start < previous:
                self.documents.write("recovery.json", {"end": scanned.isoformat()})

    def overview(self, start, end, revision=None):
        """Filter stored nonoverlapping results. Never reset either scenario or run a historical optimizer."""
        head = self.documents.read("head.json") if self.documents.exists("head.json") else None
        versions, next_revision = self.evaluation_versions()
        if revision:
            if len(revision) != SHA256_HEX_LENGTH or any(
                candidate not in "0123456789abcdef" for candidate in revision
            ):
                raise ValueError("Invalid evaluation version")
            head = self.documents.read(f"evaluation-{revision}.json")
        rows = self.store.results(head["revision"], start, end) if head else []
        summary, daily = self.store.summary(head["revision"] if head else "", start, end)
        totals = {
            scenario: {
                key: summary[f"{scenario}_{key}"]
                for key in (
                    "purchase",
                    "sale",
                    "bonus",
                    "grid_cash",
                    "wear",
                    "inventory_change",
                    "economic_cost",
                    "import_kwh",
                    "export_kwh",
                )
            }
            for scenario in ("actual", "reference")
        }
        expected = int((end - start) / SETTLEMENT_INTERVAL)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "revision": head["revision"] if head else None,
            "contract_version": head["contract_version"] if head else None,
            "issue": self.issue if not revision else None,
            "intervals": rows,
            "chart": daily,
            "details_truncated": len(rows) < summary["count"],
            "totals": totals,
            "savings": summary["savings"],
            "avoided_import_kwh": summary["avoided_import_kwh"],
            "coverage": {
                "evaluated": summary["duration_h"] / (SETTLEMENT_INTERVAL.total_seconds() / 3600),
                "expected": expected,
                "estimated": summary["estimated"],
            },
            "reference": "Simulated self-consumption without EMS",
            "versions": versions,
            "next_evaluation": next_revision,
            "corrected": bool(head and head.get("previous_revision")),
        }

    def evaluation_versions(self, before=None):
        """Page immutable correction descriptors without exposing or loading interval payloads."""
        if before and (
            len(before) != SHA256_HEX_LENGTH
            or any(candidate not in "0123456789abcdef" for candidate in before)
        ):
            raise ValueError("Invalid evaluation cursor")
        current = (
            self.documents.read(f"evaluation-{before}.json")
            if before
            else (self.documents.read("head.json") if self.documents.exists("head.json") else None)
        )
        versions = []
        while current and len(versions) < MAX_EVALUATION_VERSIONS:
            versions.append(
                {key: current.get(key) for key in ("revision", "created_at", "contract_version")}
            )
            parent = current.get("previous_revision")
            if len(versions) == MAX_EVALUATION_VERSIONS:
                return versions, parent
            current = self.documents.read(f"evaluation-{parent}.json") if parent else None
        return versions, None
