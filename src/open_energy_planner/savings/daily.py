"""Close measured local days independently of retained complete plans.

Daily reports contain compact monetary/energy totals and chronological scenario
checkpoints. Raw quarters remain temporary evidence during three complete local
recovery days. Missing evidence never becomes zero or a whole-day extrapolation.
"""

import json
from bisect import bisect_left, bisect_right
from dataclasses import asdict
from datetime import datetime, timedelta
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.contracts.wire import parse_counters
from open_energy_planner.core.calendar import day_window
from open_energy_planner.installation.location import SiteLocation

from .accounting import (
    advance_counters,
    evaluate,
    independent_account,
    inventory,
    inventory_curves,
    simulated_counters_ready,
    value_movements,
    without_battery_flows,
)
from .advice import active_advice
from .context import physical_context
from .model import POLICY_VERSION, PendingSettlement, parse_frame
from .physical import advance
from .repricing import corrected_problem
from .runtime import capture_start, settlement_issue, version
from .storage.adapters.sqlalchemy import TOTAL_KEYS, stamp

RECOVERY_CALENDAR_DAYS = 3
DAILY_POLICY_VERSION = "daily-savings-v1"
SCENARIOS = ("actual", "reference", "without_battery")
MISSING_MEASUREMENTS = "Required measurements are missing. Missing energy is not treated as zero."
MISSING_CONTEXT = "The historical installation context is unavailable."
MISSING_ACCOUNT = "Required prices, counters or physical evidence are unavailable."
UNKNOWN_REFERENCE = "Earlier missing evidence prevents a continuous battery or bonus comparison. No state was reset."
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
        "opening": None,
        "closing": None,
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
    """Add only accepted interval amounts, with explicit inventory endpoints and reconstruction quality."""
    seconds = (frame.end - frame.start).total_seconds()
    scenario["totals"] = {
        key: fsum((scenario["totals"][key] if scenario["totals"] else 0.0, result[key]))
        for key in TOTAL_KEYS
    }
    scenario["coverage_seconds"] += seconds
    scenario["estimated_seconds"] += seconds if estimated else 0.0
    if scenario["opening"] is None:
        scenario["opening"] = {
            "at": frame.start.isoformat(),
            "value": result["inventory_start"],
            "stored_kwh": result.get("opening_stored", {}),
        }
    scenario["closing"] = {
        "at": frame.end.isoformat(),
        "value": result["inventory_end"],
        "stored_kwh": result["stored"],
    }


class DailySavings:
    """Serialize one completed calendar day per worker round using the existing settlement lock."""

    def __init__(self, runtime):
        """Borrow app-owned calculation services and stores. No independent engine or scheduler is created."""
        self.runtime = runtime
        self.store = runtime.store
        self.documents = runtime.documents

    def overview(self, start, end):
        """Read daily reports and full-period SQL totals without recalculation or loading old plans."""
        summary, reports, chart = self.store.daily_overview(start, end)
        if summary["currencies"] > 1:
            raise ValueError("Choose a period with one historical currency")
        expected = (end - start).total_seconds()
        coverage = {}
        for kind in SCENARIOS:
            seconds = summary[f"{kind}_coverage_seconds"] or 0.0
            coverage[kind] = {
                "seconds": seconds,
                "expected_seconds": expected,
                "status": "complete"
                if seconds == expected
                else "partial"
                if seconds
                else "unavailable",
            }
        # Checkpoints are internal continuation, not an instruction to restart a scenario from the UI.
        reports = [
            {key: value for key, value in report.items() if key != "checkpoint"}
            for report in reports
        ]
        return {
            "daily": True,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": reports,
            "day_count": int(summary["days"]),
            "details_truncated": len(reports) < summary["days"],
            "chart": chart,
            "currency": summary["currency"],
            "coverage": coverage,
            "totals": {
                kind: {key: summary[f"{kind}_{key}"] for key in TOTAL_KEYS} for kind in SCENARIOS
            },
            **{
                key: summary[key]
                for key in (
                    "savings",
                    "avoided_import_kwh",
                    "comparison_seconds",
                    "battery_savings",
                    "battery_comparison_seconds",
                    "self_consumption_savings",
                    "self_consumption_comparison_seconds",
                )
            },
            "issue": self.runtime.issue,
        }

    def process(self, contract, book, now, location):
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
        checkpoint = (
            previous["checkpoint"] if previous and previous["end"] == start.isoformat() else None
        )
        if previous and checkpoint is None:
            raise ValueError("Daily savings calendar continuity is unavailable")
        report = self.calculate(day.isoformat(), timezone, contract, book, now, initial, checkpoint)
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

    def calculate(self, day, timezone, contract, book, now, initial, previous):
        """Evaluate original quarters in order and preserve per-scenario missing ranges and continuation.

        An absent battery/counter continuation remains unknown. A measured actual
        interval can still be billed independently when a reference is unavailable.
        Partial benefit uses only intervals accepted for both compared scenarios.
        """
        self.runtime.issue, self.runtime.attention = None, False
        self.runtime.physical_issue = None
        contexts = {}

        def get_context(identity):
            """Decode each distinct plan once during this bounded daily replay, including active advice."""
            if identity not in contexts:
                contexts[identity] = self.runtime.plans.get_context(identity)
            return contexts[identity]

        start, end = day_window(day, timezone)
        initial_plan, activations, market_rows = self.store.daily_inputs(
            start, end, contract.market_area, contract.currency
        )
        # Match SQL's canonical UTC ordering. Advice validates retained dates inside the frame.
        activation_times = [row["activated_at"] for row in activations]
        context_ids = []
        context_id = initial_plan
        for activation in activations:
            context_id = activation["plan_id"] or context_id
            context_ids.append(context_id)
        scenarios = {kind: empty_scenario() for kind in SCENARIOS}
        cursor = max(start, initial)
        if cursor > start:
            for scenario in scenarios.values():
                missing(scenario, start, min(end, cursor), "Comparison had not started yet.")
        state = previous.get("reference") if previous else None
        reference_known = previous["reference_known"] if previous else cursor == initial
        physical_state = previous.get("physical", state) if previous else None
        physical_known = (
            previous.get("physical_known", reference_known) if previous else reference_known
        )
        actual_counters = previous.get("actual_counters") if previous else {}
        reference_counters = previous.get("reference_counters") if previous else {}
        without_counters = previous.get("without_battery_counters") if previous else {}
        actual_checkpoint = previous.get("actual") if previous else None
        benefit, avoided, paired_seconds = [], [], 0.0
        battery_benefit, self_consumption_benefit = [], []
        battery_seconds = self_consumption_seconds = 0.0
        versions, names, installations, price_versions = set(), {}, {}, set()
        inventory_changes = []
        counter_seed_evidence = {}
        display_sources = {}
        energy = {
            key: {"kwh": None, "coverage_seconds": 0.0}
            for key in (
                "grid_import",
                "grid_export",
                "household",
                "solar_ac",
            )
        }
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
                    state, reference_known, actual_counters, without_counters = (
                        None,
                        False,
                        None,
                        None,
                    )
                    actual_checkpoint = None
                    reference_counters = None
                    physical_state, physical_known = None, False
                    self.runtime.physical_issue = UNKNOWN_REFERENCE
                active_index = bisect_right(activation_times, stamp(frame.start))
                plan_id = context_ids[active_index - 1] if active_index else initial_plan
                plan = get_context(plan_id) if plan_id else None
                accepted = {}
                common_start = frame.start == initial
                estimated = frame.quality != "recorded"
                pv_estimated = False
                physical, physical_advanced = None, False
                try:
                    if plan is None:
                        raise KeyError("context")
                    problem = physical_context(plan)
                    if (
                        plan.get("currency", contract.currency) != contract.currency
                        or plan.get("market_area", contract.market_area) != contract.market_area
                    ):
                        raise ValueError("Historical market identity changed")
                    original_physics = plan["input_snapshot"]["problem"]
                    installation = {
                        key: original_physics[key]
                        for key in (
                            "inverters",
                            "batteries",
                            "pv_sources",
                            "grid_import_limit_w",
                            "grid_export_limit_w",
                        )
                        if key in original_physics
                    }
                    installations[version(installation)] = installation
                    original_contract = plan["input_snapshot"].get("contract_version")
                    if original_contract and original_contract != contract.version:
                        problem, _tariffs = corrected_problem(plan, contract)
                    bindings = dict(row["context"]["bindings"])
                    if isinstance(plan.get("display_sources"), dict):
                        display_sources.update(
                            {
                                key: value
                                for key, value in plan["display_sources"].items()
                                if isinstance(key, str) and isinstance(value, str)
                            }
                        )
                    display_sources.update(
                        {
                            battery["device_id"]: battery["energy_entity"]
                            for battery in bindings["batteries"]
                            if isinstance(battery.get("device_id"), str)
                            and isinstance(battery.get("energy_entity"), str)
                        }
                    )
                    display_sources.update(
                        {
                            source["source_id"]: source["entity_id"]
                            for source in bindings["solar"]
                            if isinstance(source.get("source_id"), str)
                            and isinstance(source.get("entity_id"), str)
                        }
                    )
                    add_energy(energy, frame, bindings)
                    advice = active_advice(
                        frame,
                        activations[
                            max(0, active_index - 1) : bisect_left(
                                activation_times, stamp(frame.end)
                            )
                        ],
                        get_context,
                    )
                    bindings["curtailed"] = {key: True for key in advice["possible_curtailment"]}
                    if not physical_known and not problem.batteries and not contract.bonuses:
                        # No stock or allowance can carry an unknown quantity across this gap.
                        physical_state = state = None
                        physical_known = reference_known = True
                    previous_physical = physical_state
                    if physical_known:
                        try:
                            physical = advance(problem, frame, bindings, previous_physical)
                            physical_state = {
                                **physical["state"],
                                "inventory": {"actual": None, "reference": None},
                                "curves": inventory_curves(problem),
                                "counters": {"actual": {}, "reference": {}},
                            }
                            physical_advanced = True
                            pv_estimated = physical["estimated_pv"]
                            if not problem.batteries or problem.terminal_valuations:
                                physical_state["inventory"] = {
                                    "actual": inventory(problem, frame.battery_end),
                                    "reference": inventory(
                                        problem, physical["reference"]["stored"]
                                    ),
                                }
                        except (ValueError, KeyError, TypeError) as error:
                            physical_state, physical_known = None, False
                            self.runtime.physical_issue = settlement_issue(error)
                    # Monetary outages do not erase independently established stock or usage.
                    if (
                        not reference_known
                        and physical_advanced
                        and previous_physical
                        and all(
                            value is not None for value in previous_physical["inventory"].values()
                        )
                        and simulated_counters_ready(
                            contract, frame.start, reference_counters, common_start=common_start
                        )
                    ):
                        state = {
                            **previous_physical,
                            "counters": {
                                "actual": dict(actual_counters or {}),
                                "reference": dict(reference_counters or {}),
                            },
                        }
                        reference_known = True
                    context = row["context"]
                    seeds = (
                        parse_counters({"seeds": context["seeds"]}, now).seeds
                        if context["seeds"]
                        else ()
                    )
                    try:
                        seeds = self.runtime.bonus.prepare(
                            contract, frame, bindings, seeds, book, now, version(bindings), state
                        )
                    except PendingSettlement as error:
                        self._problem(error)
                        # Known scenario usage can continue. Unvalidated openings cannot seed it.
                        seeds = ()
                    for seed in seeds:
                        seed_record = json.loads(
                            json.dumps(asdict(seed), default=lambda value: value.isoformat())
                        )
                        counter_seed_evidence[version(seed_record)] = seed_record
                    # Retained date parsing belongs inside this frame's owned failure boundary.
                    prices = [
                        row
                        for row in market_rows
                        if datetime.fromisoformat(row["start"]) < frame.end
                        and datetime.fromisoformat(row["end"]) > frame.start
                    ]
                    site = SiteLocation(**context["location"]) if context["location"] else None
                    if reference_known and not simulated_counters_ready(
                        contract,
                        frame.start,
                        reference_counters,
                        common_start=common_start,
                    ):
                        reference_known = False
                        self._problem(
                            PendingSettlement(
                                "Simulated bonus counters are unknown after missing measurements"
                            )
                        )
                    if reference_known:
                        try:
                            opening_reference = (
                                dict(state["reference_stored"])
                                if state
                                else dict(frame.battery_start)
                            )
                            result, state = evaluate(
                                problem,
                                frame,
                                bindings,
                                state,
                                contract,
                                seeds,
                                prices,
                                site,
                                physical=physical,
                            )
                            accepted.update(
                                {kind: result[kind] for kind in ("actual", "reference")}
                            )
                            accepted["reference"]["opening_stored"] = opening_reference
                            actual_counters = state["counters"]["actual"]
                            reference_counters = state["counters"]["reference"]
                            pv_estimated = result["available_pv_estimated"]
                        except (ValueError, KeyError, TypeError) as error:
                            self._problem(error)
                            state, reference_known = None, False
                    if "actual" not in accepted:
                        try:
                            accepted["actual"], actual_counters = independent_account(
                                problem,
                                frame,
                                bindings,
                                "actual",
                                actual_counters,
                                contract,
                                seeds,
                                prices,
                                site,
                                inventory_previous=actual_checkpoint,
                            )
                        except (ValueError, KeyError, TypeError) as error:
                            self._problem(error)
                            try:
                                actual_counters = advance_counters(
                                    contract,
                                    frame,
                                    fsum(frame.channels[entity] for entity in bindings["exports"]),
                                    actual_counters,
                                    seeds,
                                    site,
                                )
                            except (ValueError, KeyError, TypeError):
                                actual_counters = None
                    if "reference" not in accepted:
                        if physical_advanced and simulated_counters_ready(
                            contract, frame.start, reference_counters, common_start=common_start
                        ):
                            try:
                                reference_counters = advance_counters(
                                    contract,
                                    frame,
                                    physical["reference"]["export_kwh"],
                                    reference_counters,
                                    seeds,
                                    site,
                                )
                            except (ValueError, KeyError, TypeError):
                                reference_counters = None
                        else:
                            reference_counters = None
                    try:
                        accepted["without_battery"], without_counters = independent_account(
                            problem,
                            frame,
                            bindings,
                            "without_battery",
                            without_counters,
                            contract,
                            seeds,
                            prices,
                            site,
                            common_start=common_start,
                        )
                    except (ValueError, KeyError, TypeError) as error:
                        self._problem(error)
                        if simulated_counters_ready(
                            contract, frame.start, without_counters, common_start=common_start
                        ):
                            try:
                                flows, _estimated = without_battery_flows(problem, frame, bindings)
                                without_counters = advance_counters(
                                    contract,
                                    frame,
                                    flows["export_kwh"],
                                    without_counters,
                                    seeds,
                                    site,
                                )
                            except (ValueError, KeyError, TypeError):
                                without_counters = None
                        else:
                            without_counters = None
                    if "actual" in accepted:
                        accepted["actual"]["opening_stored"] = dict(frame.battery_start)
                        actual_checkpoint = {
                            "end": frame.end.isoformat(),
                            "inventory": accepted["actual"]["inventory_end"],
                            "actual_stored": dict(frame.battery_end),
                            "curves": inventory_curves(problem),
                            "capacities": {
                                unit.device_id: unit.battery.capacity_kwh
                                for unit in problem.batteries
                            },
                        }
                        price_versions.update(
                            tariff["source_version"] for tariff in accepted["actual"]["tariffs"]
                        )
                    else:
                        actual_checkpoint = None
                    versions.add(
                        plan.get("input_snapshot", {}).get("contract_version") or "unrecorded"
                    )
                    if isinstance(plan.get("comparison_names"), dict):
                        names.update(
                            {
                                key: value
                                for key, value in plan["comparison_names"].items()
                                if isinstance(key, str) and isinstance(value, str)
                            }
                        )
                except (ValueError, KeyError, TypeError) as error:
                    self._problem(error)
                    state, reference_known, actual_counters, without_counters = (
                        None,
                        False,
                        None,
                        None,
                    )
                    reference_counters = None
                    if not physical_advanced:
                        physical_state, physical_known = None, False
                        self.runtime.physical_issue = settlement_issue(error)
                if physical_advanced:
                    try:
                        changed, _external = value_movements(
                            problem, previous_physical, physical["movements"]
                        )
                    except (ValueError, KeyError, TypeError):
                        changed = [
                            {**movement, "value": None} for movement in physical["movements"]
                        ]
                else:
                    changed = accepted.get("actual", {}).get("inventory_movements", [])
                inventory_changes.extend(
                    {**movement, "at": frame.start.isoformat()} for movement in changed
                )
                for kind, scenario in scenarios.items():
                    if kind in accepted:
                        include(
                            scenario,
                            accepted[kind],
                            frame,
                            estimated
                            or (kind != "actual" and pv_estimated)
                            or accepted[kind].get("available_pv_estimated", False)
                            or len(accepted[kind]["tariffs"]) > 1,
                        )
                    else:
                        reason = (
                            MISSING_CONTEXT
                            if plan is None
                            else UNKNOWN_REFERENCE
                            if kind == "reference" and not reference_known
                            else MISSING_ACCOUNT
                        )
                        missing(scenario, frame.start, frame.end, reason)
                if "actual" in accepted and "reference" in accepted:
                    benefit.append(
                        accepted["reference"]["economic_cost"] - accepted["actual"]["economic_cost"]
                    )
                    avoided.append(
                        accepted["reference"]["import_kwh"] - accepted["actual"]["import_kwh"]
                    )
                    paired_seconds += (frame.end - frame.start).total_seconds()
                if "actual" in accepted and "without_battery" in accepted:
                    battery_benefit.append(
                        accepted["without_battery"]["economic_cost"]
                        - accepted["actual"]["economic_cost"]
                    )
                    battery_seconds += (frame.end - frame.start).total_seconds()
                if "reference" in accepted and "without_battery" in accepted:
                    self_consumption_benefit.append(
                        accepted["without_battery"]["economic_cost"]
                        - accepted["reference"]["economic_cost"]
                    )
                    self_consumption_seconds += (frame.end - frame.start).total_seconds()
                cursor = frame.end
        if cursor < end:
            for scenario in scenarios.values():
                missing(scenario, cursor, end, MISSING_MEASUREMENTS)
            state, reference_known, actual_counters, without_counters = None, False, None, None
            actual_checkpoint = None
            reference_counters = None
            physical_state, physical_known = None, False
            self.runtime.physical_issue = UNKNOWN_REFERENCE
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
            "accounting_policy": POLICY_VERSION,
            "day_plan_id": self.runtime.plans.day_plan(start, end),
            "contract_version": contract.version,
            "counter_version": book.version if book else None,
            "original_contract_versions": sorted(versions),
            "installations": installations,
            "published_price_versions": sorted(price_versions),
            "currency": contract.currency,
            "market_area": contract.market_area,
            "device_names": names,
            "display_sources": display_sources,
            "energy": energy,
            "inventory_movements": inventory_changes,
            "counter_seed_evidence": counter_seed_evidence,
            "scenarios": scenarios,
            "savings": fsum(benefit) if benefit else None,
            "avoided_import_kwh": fsum(avoided) if avoided else None,
            "comparison_seconds": paired_seconds,
            "battery_savings": fsum(battery_benefit) if battery_benefit else None,
            "battery_comparison_seconds": battery_seconds,
            "self_consumption_savings": fsum(self_consumption_benefit)
            if self_consumption_benefit
            else None,
            "self_consumption_comparison_seconds": self_consumption_seconds,
            "checkpoint": {
                "end": end.isoformat(),
                "reference": state,
                "reference_known": reference_known,
                "actual_counters": actual_counters,
                "reference_counters": reference_counters,
                "without_battery_counters": without_counters,
                "actual": actual_checkpoint,
                "physical": physical_state,
                "physical_known": physical_known,
            },
        }

    def _problem(self, error):
        """Expose only fixed admitted causes, preserving the first financial failure in a daily batch."""
        if self.runtime.issue is None:
            self.runtime.issue = settlement_issue(error)
            self.runtime.attention = not isinstance(error, PendingSettlement)


def add_energy(totals, frame, bindings):
    """Retain measured energy independently of monetary coverage. Each metric keeps its own missing-data coverage."""
    members = {
        "grid_import": [(identity, 1) for identity in bindings["imports"]],
        "grid_export": [(identity, 1) for identity in bindings["exports"]],
        "household": [(term["entity_id"], term["sign"]) for term in bindings["terms"]],
        "solar_ac": [(source["entity_id"], 1) for source in bindings["solar"]],
    }
    for key, terms in members.items():
        if terms and all(identity in frame.channels for identity, _sign in terms):
            amount = fsum(frame.channels[identity] * sign for identity, sign in terms)
            if amount < 0:
                continue
            metric = totals[key]
            metric["kwh"] = fsum((metric["kwh"] or 0.0, amount))
            metric["coverage_seconds"] += (frame.end - frame.start).total_seconds()
