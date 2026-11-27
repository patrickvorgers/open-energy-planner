"""Close measured local days independently of retained complete plans.

Daily reports contain compact monetary/energy totals and calendar checkpoints.
Raw quarters remain temporary evidence during three complete local
recovery days. Missing evidence never becomes zero or a whole-day extrapolation.
"""

from datetime import datetime, timedelta
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.core.calendar import day_window
from open_energy_planner.demand.calculation.model import EnergyTerm
from open_energy_planner.installation.location import SiteLocation

from .accounting import bill, measured_cash_flows
from .model import (
    CASH_COMPONENTS,
    CASH_POLICY_VERSION,
    MISSING_MEASUREMENTS,
    PendingSettlement,
    parse_frame,
)
from .runtime import PENDING_SETTLEMENT_MESSAGES, capture_start, settlement_issue, version
from .storage.adapters.sqlalchemy import TOTAL_KEYS, stamp

RECOVERY_CALENDAR_DAYS = 3
DAILY_POLICY_VERSION = CASH_POLICY_VERSION
SCENARIOS = ("actual", "without_battery")
MISSING_ACCOUNT = "Required prices or physical evidence are unavailable."
TIMEZONE_PAUSE_MESSAGE = "Historical savings are paused because the Home Assistant timezone changed. Restore the original Home Assistant timezone to resume. Existing daily results are retained."
CONTRACT_PREREQUISITE_MESSAGE = "Contract configuration is required for settlement"
LOCATION_PREREQUISITE_MESSAGE = (
    "Installation location is required for settlement. Configure the location under Installation"
)
PREREQUISITE_MESSAGES = (CONTRACT_PREREQUISITE_MESSAGE, LOCATION_PREREQUISITE_MESSAGE)


class SavingsPrerequisiteMissing(ValueError):
    """Pause daily work with owned configuration guidance instead of reporting successful idle processing."""

    def __init__(self, *, contract_missing):
        """Select only a fixed owned message. Arbitrary exception values never become public guidance."""
        self.issue = (
            CONTRACT_PREREQUISITE_MESSAGE if contract_missing else LOCATION_PREREQUISITE_MESSAGE
        )
        super().__init__(self.issue)


class SavingsTimezoneChanged(ValueError):
    """Pause calendar-dependent processing until the original installation timezone is restored."""


def finalization_time(day, timezone):
    """Allow three complete local calendar days after the measured day, including DST transitions."""
    following = datetime.fromisoformat(day).date() + timedelta(days=RECOVERY_CALENDAR_DAYS + 1)
    return day_window(following.isoformat(), timezone)[0]


def empty_scenario():
    """Keep absent totals distinct from a valid measured zero."""
    return {
        "totals": None,
        "coverage_seconds": 0.0,
        "estimated_seconds": 0.0,
        "missing": [],
    }


def missing(scenario, start, end, reason):
    """Coalesce adjacent missing ranges with the same owned reason without losing their coverage."""
    if end <= start:
        return
    ranges = scenario["missing"]
    if ranges and ranges[-1]["end"] == start.isoformat() and ranges[-1]["reason"] == reason:
        ranges[-1]["end"] = end.isoformat()
    else:
        ranges.append({"start": start.isoformat(), "end": end.isoformat(), "reason": reason})


def include(scenario, result, frame, estimated):
    """Add accepted cash/energy totals with independent reconstruction coverage."""
    seconds = (frame.end - frame.start).total_seconds()
    scenario["totals"] = {
        key: fsum((scenario["totals"][key] if scenario["totals"] else 0.0, result[key]))
        for key in TOTAL_KEYS
        if key in result
    }
    scenario["coverage_seconds"] += seconds
    scenario["estimated_seconds"] += seconds if estimated else 0.0


def _include_matched_cash(paired, matched_components, accepted, frame, estimated):
    """Count only time and cash shared by both valid scenarios."""
    if all(kind in accepted for kind in SCENARIOS):
        for kind, components in matched_components.items():
            for component in components:
                components[component] = fsum((components[component], accepted[kind][component]))
        actual_cost = accepted["actual"]["grid_cash"]
        without_cost = accepted["without_battery"]["grid_cash"]
        for key, value in (
            ("actual_cost", actual_cost),
            ("without_battery_cost", without_cost),
            ("savings", without_cost - actual_cost),
        ):
            paired[key] = fsum((paired[key] or 0.0, value))
        seconds = (frame.end - frame.start).total_seconds()
        paired["coverage_seconds"] += seconds
        paired["estimated_seconds"] += seconds if estimated else 0.0


def _market_check(store, plans, start, end):
    """Validate a frame's market, reading historical plan metadata only when needed."""
    activations = None
    markets = {}

    def check(context, frame_start, contract):
        """Reject unresolved or mismatched market identities before billing."""
        nonlocal activations
        market = context.get("market_identity")
        if market is None:
            if activations is None:
                activations = [
                    (start, store.context_plan(start)),
                    *(
                        (datetime.fromisoformat(item["activated_at"]), item["plan_id"])
                        for item in store.activations(start, end)
                        if item["plan_id"] and datetime.fromisoformat(item["activated_at"]) > start
                    ),
                ]
            identity = next(
                (plan_id for instant, plan_id in reversed(activations) if instant <= frame_start),
                None,
            )
            if identity not in markets:
                original = plans.get_context(identity) if identity else None
                markets[identity] = (
                    {key: original[key] for key in ("currency", "market_area") if key in original}
                    if original
                    else {}
                )
            market = markets[identity]

        if not all(key in market for key in ("currency", "market_area")):
            raise PendingSettlement("Historical market identity is unresolved")
        if any(
            market.get(key, getattr(contract, key)) != getattr(contract, key)
            for key in ("currency", "market_area")
        ):
            raise PendingSettlement("Historical market identity changed")

    return check


class DailySavings:
    """Serialize one completed calendar day per worker round using the existing settlement lock."""

    def __init__(self, runtime):
        """Borrow app-owned calculation services and stores. No independent engine or scheduler is created."""
        self.runtime = runtime
        self.store = runtime.store
        self.documents = runtime.documents

    def overview(self, start, end):
        """Read matching measured-cash totals without mixing legacy economic/reference policies."""
        cash, reports, count = self.store.daily_overview(start, end)
        reports = [
            {key: value for key, value in report.items() if key != "checkpoint"}
            for report in reports
        ]
        return {
            "daily": True,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": reports,
            "day_count": count,
            "details_truncated": len(reports) < count,
            "currency": cash["currency"],
            "cash": cash,
            "issue": self.runtime.issue,
        }

    def process(self, contract, now, location):
        """Revisit provisional days chronologically, then seal due days before any retention is allowed."""
        first = self.store.first_activation()
        if first is None:
            return None
        if contract is None or location is None:
            raise SavingsPrerequisiteMissing(contract_missing=contract is None)
        timezone = location.timezone
        zone = ZoneInfo(timezone)
        today = now.astimezone(zone).date()
        progress = (
            self.documents.read("daily-progress.json")
            if self.documents.exists("daily-progress.json")
            else None
        )
        if progress and progress["timezone"] != timezone:
            # Historical calendar identities cannot be silently reassigned to another day.
            raise SavingsTimezoneChanged
        if (
            self.runtime.issue == TIMEZONE_PAUSE_MESSAGE
            or self.runtime.issue in PREREQUISITE_MESSAGES
        ):
            self.runtime.issue, self.runtime.attention = None, False
        if not self.documents.exists("daily-upgrade.json"):
            self.documents.write(
                "daily-upgrade.json", {"day": today.isoformat(), "timezone": timezone}
            )
        upgrade = self.documents.read("daily-upgrade.json")
        initial = capture_start(datetime.fromisoformat(first))
        day = (
            datetime.fromisoformat(progress["next"]).date()
            if progress
            else initial.astimezone(zone).date()
        )
        if day >= today:
            provisional = self.store.day(finalized=False)
            latest = self.store.day(latest=True)
            day = (
                datetime.fromisoformat(provisional["day"]).date()
                if provisional
                else datetime.fromisoformat(latest["day"]).date() + timedelta(days=1)
                if latest
                else initial.astimezone(zone).date()
            )
        if day >= today:
            return None
        start, _end = day_window(day.isoformat(), timezone)
        previous = self.store.day(before=start, latest=True)
        if previous and previous["end"] != start.isoformat():
            raise ValueError("Daily savings calendar continuity is unavailable")
        report = self.calculate(day.isoformat(), timezone, contract, now, initial)
        report["finalizes_after"] = max(
            finalization_time(day.isoformat(), timezone),
            finalization_time(upgrade["day"], timezone),
        ).isoformat()
        report["finalized_at"] = (
            now.isoformat()
            if (
                now >= datetime.fromisoformat(report["finalizes_after"])
                and (previous is None or previous["finalized_at"] is not None)
            )
            else None
        )
        report["evidence_version"] = version(
            {
                key: value
                for key, value in report.items()
                if key not in ("evaluated_at", "finalized_at")
            }
        )
        current = self.store.day(before=_end, latest=True)
        unchanged = (
            current
            and current["start"] == report["start"]
            and current.get("evidence_version") == report["evidence_version"]
        )
        if unchanged:
            report["evaluated_at"] = current["evaluated_at"]
        self.store.put_day(report, self.documents, (day + timedelta(days=1)).isoformat())
        if self.runtime.events and (
            not unchanged or bool(current["finalized_at"]) != bool(report["finalized_at"])
        ):
            from ..activities.events import Outcome

            with self.runtime.events.thread_attempt(
                "settlement",
                "settlement",
                "Publish daily savings",
                context={"start": report["start"], "end": report["end"]},
                failure="Daily savings publication could not be recorded. Check storage diagnostics.",
            ) as identity:
                self.runtime.events.finish_thread(
                    identity,
                    Outcome.COMPLETED,
                    "Daily savings were finalized with their recorded coverage. Missing evidence remains visible."
                    if report["finalized_at"]
                    else "Provisional daily savings were updated. Missing evidence remains visible during recovery.",
                    usable=True,
                )
        return report

    def calculate(self, day, timezone, contract, now, initial):
        """Estimate costs independently for each measured quarter.

        Apply the dated bonus rate and payment conditions without the export
        volume cap. Missing evidence affects only the quarter that requires it.
        """
        self.runtime.issue, self.runtime.attention = None, False
        start, end = day_window(day, timezone)
        market_rows = self.store.published(start, end, contract.market_area, contract.currency)
        scenarios = {kind: empty_scenario() for kind in SCENARIOS}
        cursor = max(start, initial)
        if cursor > start:
            for scenario in scenarios.values():
                missing(scenario, start, min(end, cursor), "Comparison had not started yet.")
        paired = {
            "actual_cost": None,
            "without_battery_cost": None,
            "savings": None,
            "coverage_seconds": 0.0,
            "estimated_seconds": 0.0,
        }
        matched_components = {kind: dict.fromkeys(CASH_COMPONENTS, 0.0) for kind in SCENARIOS}
        energy = {
            key: {"kwh": None, "coverage_seconds": 0.0}
            for key in ("grid_import", "grid_export", "household", "solar_ac")
        }
        price_versions = set()
        check_market = _market_check(self.store, self.runtime.plans, start, end)
        while cursor < end:
            rows = self.store.frames(cursor, end=end)
            if not rows:
                break
            for row in rows:
                frame = parse_frame(row["frame"])
                if frame.start < cursor or frame.end > end:
                    raise ValueError("Daily settlement frames overlap calendar boundaries")
                if frame.start > cursor:
                    for scenario in scenarios.values():
                        missing(scenario, cursor, frame.start, MISSING_MEASUREMENTS)
                accepted, missing_reasons = {}, {}
                context = row["context"]
                try:
                    bindings = context["bindings"]
                    add_energy(energy, frame, bindings)
                    check_market(context, frame.start, contract)
                    self._bill_frame(
                        contract,
                        frame,
                        context,
                        bindings,
                        market_rows,
                        accepted,
                        missing_reasons,
                        price_versions,
                    )
                except (ValueError, KeyError, TypeError) as error:
                    reason = self._problem(error)
                    missing_reasons = dict.fromkeys(SCENARIOS, reason)
                estimated = frame.quality != "recorded" or any(
                    len(result["tariffs"]) > 1 for result in accepted.values()
                )
                for kind, scenario in scenarios.items():
                    if kind in accepted:
                        include(scenario, accepted[kind], frame, estimated)
                    else:
                        missing(
                            scenario,
                            frame.start,
                            frame.end,
                            missing_reasons.get(kind, MISSING_ACCOUNT),
                        )
                _include_matched_cash(paired, matched_components, accepted, frame, estimated)
                cursor = frame.end
        if cursor < end:
            for scenario in scenarios.values():
                missing(scenario, cursor, end, MISSING_MEASUREMENTS)
        expected = (end - start).total_seconds()
        for scenario in scenarios.values():
            scenario["status"] = (
                "complete"
                if scenario["coverage_seconds"] == expected
                else "partial"
                if scenario["coverage_seconds"]
                else "unavailable"
            )
        return {
            "day": day,
            "timezone": timezone,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "expected_seconds": expected,
            "evaluated_at": now.isoformat(),
            "finalized_at": None,
            "policy": DAILY_POLICY_VERSION,
            "accounting_policy": DAILY_POLICY_VERSION,
            "day_plan_id": self.runtime.plans.day_plan(start, end),
            "contract_version": contract.version,
            "published_price_versions": sorted(price_versions),
            "currency": contract.currency,
            "market_area": contract.market_area,
            "energy": energy,
            "scenarios": scenarios,
            "cash": paired,
            "matched_components": matched_components if paired["coverage_seconds"] else None,
            "checkpoint": {"end": end.isoformat()},
        }

    def _bill_frame(
        self,
        contract,
        frame,
        context,
        bindings,
        market_rows,
        accepted,
        missing_reasons,
        price_versions,
    ):
        """Price each available scenario while retaining independent failures."""
        flows = {}
        flows["actual"] = (
            fsum(frame.channels[entity] for entity in bindings["imports"]),
            fsum(frame.channels[entity] for entity in bindings["exports"]),
        )
        try:
            flows.update(measured_cash_flows(frame, bindings))
        except (ValueError, KeyError, TypeError) as error:
            missing_reasons["without_battery"] = self._problem(error)
        frame_start, frame_end = stamp(frame.start), stamp(frame.end)
        prices = [
            price
            for price in market_rows
            if price["start"] < frame_end and price["end"] > frame_start
        ]
        site = SiteLocation(**context["location"]) if context["location"] else None
        for kind, (imported, exported) in flows.items():
            try:
                result = bill(
                    contract,
                    frame,
                    imported,
                    exported,
                    None,
                    (),
                    prices,
                    site,
                    uncapped=True,
                )
                result.update(import_kwh=imported, export_kwh=exported)
                accepted[kind] = result
                price_versions.update(
                    tariff["source_version"] for tariff in accepted[kind]["tariffs"]
                )
            except (ValueError, KeyError, TypeError) as error:
                missing_reasons[kind] = self._problem(error)

    def _problem(self, error):
        """Return owned interval guidance and preserve the first failure for runtime status."""
        issue = settlement_issue(error)
        if self.runtime.issue is None:
            self.runtime.issue = issue
            self.runtime.attention = not isinstance(error, PendingSettlement)
        return PENDING_SETTLEMENT_MESSAGES.get(issue, MISSING_ACCOUNT)


def add_energy(totals, frame, bindings):
    """Retain measured energy independently of monetary coverage. Each metric keeps its own missing-data coverage."""
    if not isinstance(bindings, dict) or any(
        not isinstance(bindings[name], list) for name in ("imports", "exports", "terms", "solar")
    ):
        raise ValueError("Invalid measured energy bindings")
    members = {
        "grid_import": [EnergyTerm(identity, 1) for identity in bindings["imports"]],
        "grid_export": [EnergyTerm(identity, 1) for identity in bindings["exports"]],
        "household": [EnergyTerm(**term) for term in bindings["terms"]],
        "solar_ac": [EnergyTerm(source["entity_id"], 1) for source in bindings["solar"]],
    }
    amounts = {}
    for key, terms in members.items():
        if len({term.entity_id for term in terms}) != len(terms):
            raise ValueError("Duplicate measured energy bindings")
        if key == "solar_ac" and any(
            source.get("measurement_boundary", "ac") != "ac" for source in bindings["solar"]
        ):
            continue
        if terms and all(term.entity_id in frame.channels for term in terms):
            amount = fsum(frame.channels[term.entity_id] * term.sign for term in terms)
            if amount < 0:
                continue
            amounts[key] = amount
    for key, amount in amounts.items():
        metric = totals[key]
        metric["kwh"] = fsum((metric["kwh"] or 0.0, amount))
        metric["coverage_seconds"] += (frame.end - frame.start).total_seconds()
