"""App-owned automatic model preferences backed by nonoverlapping shadow decisions."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event

from open_energy_planner.forecast_selection.calculation.decision import learned_is_better
from open_energy_planner.optimization.adapters.process.worker import run_child
from open_energy_planner.solar.calculation.model import version

from ..savings.model import SETTLEMENT_INTERVAL
from ..savings.runtime import quarter
from .policy import (
    COMPARISON_SCENARIOS,
    MAX_REALIZATION_FRAMES,
    MIN_DECISION_EVALUATION_WINDOWS,
    MIN_DECISION_WINDOW_HOURS,
    SELECTION_POLICY,
)
from .worker import (
    SHADOW_CAPTURE_TIMEOUT_SECONDS,
    SHADOW_EVALUATION_TIMEOUT_SECONDS,
    calculate_in_child,
)

EVIDENCE_WAIT_HOURS = 24
FAILED_CAPTURE_RETRY_MINUTES = 5


def variants(installation, demand, solar, contract, strategy, counter_version=None):
    """Describe each existing challenger without adding user-facing ML configuration."""
    context = {
        "policy": SELECTION_POLICY,
        "installation": installation.version,
        "demand": demand.configuration.version,
        "solar": solar.configuration.version,
        "contract": contract.version,
        "counters": counter_version,
        "strategy": str(strategy),
        "location": asdict(solar.location) if solar.location else None,
    }
    rows = [
        {
            "scope": "demand",
            "label": "Household consumption",
            "kind": "demand",
            "baseline": demand.baseline,
            "candidate": demand.candidate,
            "forecast_scope": demand.candidate.scope if demand.candidate else None,
            "algorithm": (demand.candidate.model if demand.candidate else "ridge-v2")
            + ":"
            + getattr(demand, "candidate_features", "time"),
        }
    ]
    calibrated = {row.source_id for row in solar.calibrations}
    for source in solar.configuration.sources:
        rows.append(
            {
                "scope": "solar:" + source.source_id,
                "label": "Solar: " + source.source_id,
                "kind": "solar",
                "source_id": source.source_id,
                "baseline": solar.baseline,
                "candidate": solar.candidate if source.source_id in calibrated else None,
                "algorithm": solar.candidate.model if solar.candidate else "pvwatts-calibrated-v2",
            }
        )
    inverters = {row.inverter_id: row for row in solar.configuration.inverters}
    blocker = (
        "Available PV needs uncurtailed, independent production evidence"
        if any(
            source.can_curtail or inverters[source.inverter_id].hybrid
            for source in solar.configuration.sources
        )
        else None
    )
    if demand.candidate and demand.candidate.excluded_devices:
        blocker = "Controlled-load realization is required for model comparison"
    return [
        {
            **row,
            "eligibility_issue": blocker,
            "family_id": version(
                {
                    **context,
                    "scope": row["scope"],
                    "algorithm": row["algorithm"],
                    "forecast_scope": row.get("forecast_scope"),
                }
            ),
        }
        for row in rows
    ]


class ModelSelection:
    """Own SQL evidence and cached UI status independently of command publication.

    One app worker invokes process off the event loop. Native work is isolated in
    bounded spawned children. Capture/evaluation failures never revoke a usable
    real plan or block its publication. Learned preferences require checked evidence.
    """

    def __init__(self, store, plans, settlements, contracts, counters):
        """Restore durable comparisons while keeping active model preference a checked derived value."""
        self.store, self.plans, self.settlements, self.contracts = (
            store,
            plans,
            settlements,
            contracts,
        )
        self.counters = counters
        self.stop = Event()
        self.rows = []
        self.preferences = {}
        self.issue = store.issue

    def process(self, original, choices, now):
        """Advance at most one expensive capture/evaluation per poll and refresh all current statuses."""
        if self.store.issue:
            self.preferences = {}
            self.issue = self.store.issue
            return
        worked = False
        rows, preferences = [], {}
        for choice in choices:
            identity = choice["family_id"]
            family = deepcopy(
                self.store.state["families"].get(identity, {"windows": [], "pending": None})
            )
            if (
                not worked
                and not choice["eligibility_issue"]
                and choice["candidate"] is not None
                and choice["baseline"] is not None
            ):
                worked = self._advance(original, choice, family, now)
                family = self.store.state["families"].get(identity, family)
            windows = [self.store.read("evaluation", reference) for reference in family["windows"]]
            preferred = not choice["eligibility_issue"] and learned_is_better(
                windows, original["strategy"]
            )
            preferences[identity] = preferred
            pending = (
                self.store.read("capture", family["pending"]) if family.get("pending") else None
            )
            rows.append(
                {
                    "family_id": identity,
                    "scope": choice["scope"],
                    "label": original.get("comparison_names", {}).get(
                        choice.get("source_id"), choice["label"]
                    ),
                    "currency": original["currency"],
                    "preferred": "learned" if preferred else "baseline",
                    "completed_windows": len(windows),
                    "required_windows": MIN_DECISION_EVALUATION_WINDOWS,
                    "pending_until": pending["end"] if pending else None,
                    "status": "training_history_required"
                    if choice["candidate"] is None
                    else "learned_preferred"
                    if preferred
                    else "collecting_decision_evidence"
                    if len(windows) < MIN_DECISION_EVALUATION_WINDOWS
                    else "no_consistent_decision_benefit",
                    "issue": choice["eligibility_issue"] or family.get("issue"),
                    "evaluated_at": windows[-1]["evaluated_at"] if windows else None,
                    "windows": [
                        {
                            "start": window["start"],
                            "end": window["end"],
                            "evaluated_at": window["evaluated_at"],
                            **{
                                scenario: {
                                    key: window[scenario][key]
                                    for key in ("economic_cost", "corrected_import_kwh")
                                }
                                for scenario in COMPARISON_SCENARIOS
                            },
                        }
                        for window in windows
                    ],
                }
            )
        self.rows, self.preferences, self.issue = rows, preferences, None

    def _advance(self, original, choice, family, now):
        """Retain successful comparisons, retry incomplete evidence and bound failed-window waiting."""
        identity = choice["family_id"]
        pending = family.get("pending")
        if family.get("retry_after") and now < datetime.fromisoformat(family["retry_after"]):
            return False
        if pending:
            capture = self.store.read("capture", pending)
            start, end = (datetime.fromisoformat(capture[key]) for key in ("start", "end"))
            if now < end:
                return False
            archived = self.plans.get_plan(capture["original_plan_id"])
            counter_versions = {
                row["counter_version"]
                for row in archived["input_snapshot"].get("bonus_evidence", ())
            }
            if len(counter_versions) > 1:
                raise ValueError("Opening bonus evidence has inconsistent versions")
            seeds = (
                self.counters.load(next(iter(counter_versions))).seeds if counter_versions else ()
            )
            result = self._calculate(
                {
                    "operation": "evaluate",
                    "capture": capture,
                    "original": archived,
                    "records": self.settlements.frames(
                        start, end=end, limit=MAX_REALIZATION_FRAMES + 1
                    ),
                    "market_rows": self.settlements.published(
                        start, end, original["market_area"], original["currency"]
                    ),
                    "contract": self.contracts.load(capture["contract_version"]),
                    "seeds": seeds,
                }
            )
            if result["status"] == "pending" and now < end + timedelta(hours=EVIDENCE_WAIT_HOURS):
                family["issue"] = result["issue"]
                family["retry_after"] = (
                    now + timedelta(minutes=FAILED_CAPTURE_RETRY_MINUTES)
                ).isoformat()
                self.store.commit(identity, family)
                return True
            evaluation = {
                "capture": pending,
                "evaluated_at": now.isoformat(),
                "family_id": identity,
            }
            if result["status"] == "ok":
                evaluation.update(result["value"])
                family["windows"] = (family["windows"] + [version(evaluation)])[
                    -MIN_DECISION_EVALUATION_WINDOWS:
                ]
                family["issue"] = None
            else:
                evaluation["issue"] = result["issue"]
                family["issue"] = result["issue"]
                family["last_failure"] = version(evaluation)
            family["pending"] = None
            family["retry_after"] = None
            self.store.commit(identity, family, evaluation=evaluation)
            return True
        if family.get("retry_after") and now < datetime.fromisoformat(family["retry_after"]):
            return False
        controlled = original.get("input_snapshot", {}).get("problem", {})
        if (
            controlled.get("ev_work")
            or controlled.get("appliances")
            or controlled.get("thermal_work")
        ):
            family["issue"] = "Controlled-load realization is required for new model comparisons"
            family["retry_after"] = (
                now + timedelta(minutes=FAILED_CAPTURE_RETRY_MINUTES)
            ).isoformat()
            self.store.commit(identity, family)
            return True
        if now >= datetime.fromisoformat(original["valid_until"]):
            return False
        last = original["input_snapshot"]["problem"]["intervals"][-1]
        end = datetime.fromisoformat(last["start"]) + timedelta(hours=last["duration_h"])
        start = quarter(now) + SETTLEMENT_INTERVAL
        if end - start < timedelta(hours=MIN_DECISION_WINDOW_HOURS):
            family["issue"] = "A full-horizon plan is required for model comparison"
            family["retry_after"] = (
                now + timedelta(minutes=FAILED_CAPTURE_RETRY_MINUTES)
            ).isoformat()
            self.store.commit(identity, family)
            return True
        result = self._calculate(
            {"operation": "capture", "original": original, "variant": choice, "now": now}
        )
        if result["status"] != "ok":
            family["issue"] = result["issue"]
            family["retry_after"] = (
                now + timedelta(minutes=FAILED_CAPTURE_RETRY_MINUTES)
            ).isoformat()
            self.store.commit(identity, family)
            return True
        capture = result["value"]
        family.update(pending=version(capture), issue=None, retry_after=None)
        self.store.commit(identity, family, capture=capture)
        return True

    def _calculate(self, payload):
        """Translate bounded process failure into explicit absent selection evidence, never a fabricated score."""
        try:
            timeout = (
                SHADOW_EVALUATION_TIMEOUT_SECONDS
                if payload["operation"] == "evaluate"
                else SHADOW_CAPTURE_TIMEOUT_SECONDS
            )
            result = run_child(calculate_in_child, payload, timeout, cancel=self.stop)
            if not isinstance(result, dict) or result.get("status") not in (
                "ok",
                "pending",
                "failed",
            ):
                raise ValueError("Invalid shadow worker result")
            return result
        except TimeoutError:
            return {"status": "pending", "issue": "Shadow worker timed out. Retained for retry"}
        except (OSError, RuntimeError, ValueError):
            if self.stop.is_set():
                return {
                    "status": "pending",
                    "issue": "Comparison interrupted for shutdown. Retained for retry",
                }
            return {
                "status": "failed",
                "issue": "Shadow worker unavailable. No new preference evidence",
            }

    def document(self, active):
        """Separate validated preference from the model actually used by the current forecast."""
        return {
            "policy": SELECTION_POLICY,
            "issue": self.issue,
            "explanation": "Household model selection requires three nonoverlapping realized decision windows. Old HA history trains models but cannot invent old planning knowledge. Shadow costs are simulated decisions, not measured EMS savings. Solar source selection separately compares matched forward forecast accuracy.",
            "models": [{**row, "active_model": active.get(row["scope"])} for row in self.rows],
        }
