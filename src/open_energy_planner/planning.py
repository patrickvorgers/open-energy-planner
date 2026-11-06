"""Application planning use case: immutable inputs, independent checks, publication gate."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from time import monotonic
from typing import Protocol

from open_energy_planner.core.units import FULL_SOC_PERCENT, SECONDS_PER_MINUTE
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.optimization.calculation.consumption import (
    CONSUMPTION_PRIORITY_TOLERANCE_KWH,
    CONSUMPTION_VALIDATION_TOLERANCE_KWH,
    evaluate_consumption,
)

from .installation.model import InstallationProblem
from .installation.validation import DEFAULT_VALIDATION_TOLERANCE, validate
from .optimization.domain import SolveResult, Status, Strategy
from .runtime.diagnostics.logging import failure_trace, owned_validation_message
from .runtime.diagnostics.planning_notifications import explain_planning_problem

_LOGGER = logging.getLogger(__name__)

# Fixed reasons from independent installation/EV/thermal validation and command mapping.
VALIDATION_MESSAGES = (
    "AC conservation",
    "Appliance start identities",
    "Appliance time window",
    "Aware appliance start",
    "Battery identities",
    "Battery reserve",
    "Battery terminal bounds",
    "Bonus result shape",
    "Bonus settlement",
    "Clipping requires saturated AC output",
    "Clipping requires saturated storage acceptance",
    "Committed appliance disappeared",
    "Committed appliance start changed",
    "Control group command changed",
    "Converter bounds",
    "DC conservation",
    "Disabled PV produces",
    "EV AC energy",
    "EV command group",
    "EV conversion energy",
    "EV finite nonnegative charge",
    "EV fixed power",
    "EV identities",
    "EV interrupted command group",
    "EV minimum power",
    "EV multiple partial stops",
    "EV partial stop",
    "EV power limit",
    "EV target overcharge",
    "EV time window",
    "Financial totals",
    "Finite battery energy",
    "Fractional device command",
    "Grid bound",
    "Grid direction",
    "Installation plan shape",
    "Inverter identities",
    "Missing bonus evidence",
    "PV and battery command identities collide",
    "PV available energy",
    "PV control permission",
    "PV identities",
    "Stored energy conservation",
    "Thermal AC energy",
    "Thermal command identity collides with another device",
    "Thermal finite physical evidence",
    "Thermal fixed heater power",
    "Thermal heat balance",
    "Thermal heating before readiness",
    "Thermal heating before state admission",
    "Thermal identities",
    "Thermal physical bounds",
    "Thermal physical shortfall",
    "Thermal quarter control",
    "Thermal refined control interval",
    "Thermal unmet draw before state admission",
    "Thermal unmet draw requires exhausted heat",
    "Undeclared PV clipping",
    "Whole-watt command",
)


def encode(value):
    """Encode aware snapshot datetimes as ISO strings. Reject unsupported JSON values."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


@dataclass(frozen=True)
class InputSnapshot:
    """Freeze physical inputs, strategy, source versions and evidence for one round.

    ``maximum_input_age_s`` bounds measurement admission during calculation.
    ``future_valid_until`` is an optional timezone-aware instant (not necessarily
    UTC) that independently limits published command validity to actual forecast
    or provider expiry. When None, plan end and ``command_deadline`` determine
    that bound. It does not extend measurement admission age.
    """

    snapshot_id: str
    config_revision: str
    measured_at: datetime
    measurement_version: str
    forecast_version: str
    price_version: str
    problem: InstallationProblem
    strategy: str = "minimize cost"
    maximum_input_age_s: float = SECONDS_PER_MINUTE
    source: str = "synthetic"
    measurement_evidence: tuple = ()
    source_configuration: tuple = ()
    measurement_issues: tuple = ()
    contract_version: str | None = None
    tariff_evidence: tuple = ()
    bonus_evidence: tuple = ()
    site_location: SiteLocation | None = None
    demand_evidence: tuple = ()
    solar_evidence: tuple = ()
    future_valid_until: datetime | None = None
    appliance_revision: str | None = None
    appliance_evidence: tuple = ()
    ev_revision: str | None = None
    ev_evidence: tuple = ()
    thermal_revision: str | None = None
    thermal_evidence: tuple = ()

    def __post_init__(self):
        """Require explicit versions, aware time, a domain problem and a positive age bound."""
        if self.future_valid_until is not None and self.future_valid_until.utcoffset() is None:
            raise ValueError("Future input expiry requires a timezone")
        if self.measured_at.utcoffset() is None:
            raise ValueError("Snapshot timestamp requires a timezone")
        for name in (
            "snapshot_id",
            "config_revision",
            "measurement_version",
            "forecast_version",
            "price_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError("Snapshot version references must be explicit")
        if not isinstance(self.problem, InstallationProblem) or not isinstance(
            self.problem.intervals, tuple
        ):
            raise ValueError("A frozen installation input snapshot is required")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
        if not isfinite(self.maximum_input_age_s) or self.maximum_input_age_s <= 0:
            raise ValueError("Snapshot age bound must be finite and positive")

    def record(self):
        """Return a detached JSON-compatible representation without mutating the original snapshot."""
        return json.loads(json.dumps(asdict(self), default=encode, allow_nan=False))

    @property
    def digest(self):
        """Hash the canonical snapshot record to identify its exact planning inputs."""
        body = json.dumps(self.record(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def current_at(self, now):
        """Require fresh measurements and a live candidate's first transfer interval still usable.

        Later steps assume the first planned energy transfer occurred. Reject a live
        candidate that finishes after that interval, without shortening an already
        accepted plan's command validity to its first step.
        """
        first = self.problem.intervals[0]
        return 0 <= (now - self.measured_at).total_seconds() < self.maximum_input_age_s and (
            self.source != "home_assistant" or now < first.start + timedelta(hours=first.duration_h)
        )


class Solver(Protocol):
    """Define asynchronous solving without leaking native solver objects into orchestration."""

    async def solve(self, problem: InstallationProblem, *, strategy=Strategy.COST) -> SolveResult:
        """Solve asynchronously using the requested strategy, defaulting to cost.

        Non-cost implementations must accept the strategy keyword. The service
        omits it on the cost path to retain existing cost-only test adapters.
        """
        ...


@dataclass(frozen=True)
class PlanningOutcome:
    """Pair a complete validated wire document, if any, with an explicit calculation reason.

    ``result`` retains the solver outcome for calculation history. It is None
    when no solver ran or no result returned. It can remain present after a
    candidate is rejected or its inputs expire, while ``document`` is None.
    """

    document: dict | None
    reason: str
    result: SolveResult | None = None


class CommandMapper:
    """Project independently checked installation flows into per-device command proposals."""

    def build(self, snapshot, result, costs, generated_at):
        """Delegate validated installation flows to a schema-2 command document.

        The caller owns independent physical and financial validation. After checking
        the installation input type, this method delegates conversion and envelope
        construction to ``installation.commands.plan_document``. Fixed battery targets
        convert kWh over interval hours to terminal DC watts, positive for discharge
        and negative for charging. They must be whole watts, while grid following
        has no fixed target. PV permissions and EV/thermal consumption commands are
        mapped by that same delegate. Validity is bounded by the plan horizon,
        publication deadline and available future-input expiry.

        This creates a new document and plan UUID without changing the inputs,
        persisting state or executing physical equipment.

        Args:
            snapshot: InputSnapshot containing the InstallationProblem and frozen
                configuration, measurement, forecast and tariff evidence.
            result: SolveResult containing a non-None InstallationPlan that has
                already passed independent physical and financial validation.
            costs: Independently recomputed Costs in the contract currency, retained
                separately from physical energy and power quantities.
            generated_at: Timezone-aware publication time for the document and its
                bounded command deadline.

        Returns:
            A new plain schema-2 dictionary with typed per-device command proposals,
            original input evidence, strategy and independently recomputed costs.

        Raises:
            TypeError: The snapshot does not contain an InstallationProblem.
            ValueError: Delegated conversion finds a fractional fixed battery target,
                mismatched interval/device counts or colliding command identities.
        """
        if not isinstance(snapshot.problem, InstallationProblem):
            raise TypeError("Device commands require an installation problem")
        from .installation.commands import plan_document

        return plan_document(snapshot, result, costs, generated_at)


class PlanningService:
    """Calculate and independently validate complete candidates before wire projection."""

    def __init__(self, solver: Solver, *, validator=None, clock=None):
        """Bind a domain solver, independent validator and optional deterministic test clock."""
        self.solver = solver
        self.validator = validator if validator is not None else validate
        self.clock = clock or (lambda: datetime.now(UTC))

    async def calculate(self, snapshot: InputSnapshot) -> PlanningOutcome:
        """Reject stale/unsupported input and invalid candidates before returning a full document.

        Recheck input age after solving and validation. Consumption requires a
        proved primary optimum. A feasible financial result is not cost optimal.
        """
        if snapshot.strategy not in tuple(Strategy):
            return PlanningOutcome(None, "unsupported_strategy")
        if not snapshot.current_at(self.clock()):
            return PlanningOutcome(None, "stale_input")
        result = None
        try:
            result = (
                await self.solver.solve(snapshot.problem)
                if snapshot.strategy == Strategy.COST
                else await self.solver.solve(snapshot.problem, strategy=Strategy.CONSUMPTION)
            )
            if result.status not in (Status.OPTIMAL, Status.FEASIBLE) or result.plan is None:
                return PlanningOutcome(None, result.stop_reason, result)
            costs = await asyncio.to_thread(self.validator, snapshot.problem, result.plan)
            metrics = evaluate_consumption(snapshot.problem, result.plan)
            if snapshot.strategy == Strategy.CONSUMPTION and (
                result.consumption_optimum_kwh is None
                or not isfinite(result.consumption_optimum_kwh)
                or result.consumption_optimum_kwh < 0
                or abs(metrics.corrected_import_kwh - result.consumption_optimum_kwh)
                > CONSUMPTION_PRIORITY_TOLERANCE_KWH + CONSUMPTION_VALIDATION_TOLERANCE_KWH
            ):
                return PlanningOutcome(None, "consumption_objective_rejected", result)
            now = self.clock()
            if not snapshot.current_at(now):
                return PlanningOutcome(None, "input_expired_during_solve", result)
            document = CommandMapper().build(snapshot, result, costs, now)
            if snapshot.strategy == Strategy.CONSUMPTION:
                document["consumption_objective"] = {
                    **asdict(metrics),
                    "corrected_import_kwh": metrics.corrected_import_kwh,
                    "proven_optimum_kwh": result.consumption_optimum_kwh,
                    "tolerance_kwh": CONSUMPTION_PRIORITY_TOLERANCE_KWH,
                    "terminal_method": "initial_stock_grid_replacement",
                }
            return PlanningOutcome(document, "validated", result)
        except ValueError as error:
            _LOGGER.warning(
                "Calculated plan failed validation (%s). No partial plan will be published. Check the input and solver diagnostics.",
                owned_validation_message(error, VALIDATION_MESSAGES)
                or "unrecognized candidate data",
            )
            _LOGGER.debug(
                "Rejected planning candidate diagnostics (%s).\n%s",
                type(error).__name__,
                failure_trace(error),
            )
            return PlanningOutcome(None, "candidate_rejected", result)
        except (TypeError, RuntimeError, IndexError, KeyError):
            _LOGGER.exception(
                "Plan calculation failed unexpectedly. No partial plan will be published. Check the solver diagnostics."
            )
            return PlanningOutcome(None, "candidate_rejected", result)


class PublicationSuperseded(Exception):
    """Publication revalidation rejected inputs that changed or expired after calculation."""


def battery_dispatch_available(document, original, current):
    """Check remaining valid dispatch against measured storage, without treating forecast drift as a fault.

    Use validated internal-energy changes, including losses, only until command expiry.
    Later horizon steps will be reconsidered by subsequent rounds. Physical protection
    and fast grid-following remain owned by HA and equipment.
    """
    before = {
        battery_item.device_id: battery_item.battery.initial_kwh
        for battery_item in original.problem.batteries
    }
    batteries = {
        battery_item.device_id: battery_item.battery for battery_item in current.problem.batteries
    }
    if before.keys() != batteries.keys():
        return False
    energy = {key: battery.initial_kwh for key, battery in batteries.items()}
    until = datetime.fromisoformat(document["valid_until"])
    for interval, flow in zip(original.problem.intervals, document["expected_flows"], strict=True):
        start, end = interval.start, interval.start + timedelta(hours=interval.duration_h)
        if start >= until:
            break
        fraction = (
            max(0, (min(end, until) - max(start, current.measured_at)).total_seconds())
            / (end - start).total_seconds()
        )
        ends = {
            battery_item["device_id"]: battery_item["end_kwh"] for battery_item in flow["batteries"]
        }
        for key, battery in batteries.items():
            floor = min(battery.minimum_kwh, energy[key])
            energy[key] += (ends[key] - before[key]) * fraction
            if (
                not floor - DEFAULT_VALIDATION_TOLERANCE
                <= energy[key]
                <= battery.capacity_kwh + DEFAULT_VALIDATION_TOLERANCE
            ):
                return False
        before = ends
    return True


class PlanningCoordinator:
    """Finish valid calculations while coalescing ordinary updates into the next round.

    ``block_reason`` retains the current blocker during recovery calculation.
    ``blocked_since`` is the aware start of the current process-local blocked episode.
    It remains set while calculating and clears only after successful publication.
    It is None before the first blocker and resets with the process.
    """

    def __init__(
        self,
        service: PlanningService,
        publish: Callable[[dict], Awaitable[None]],
        *,
        clock=None,
        explain=None,
        record_calculation=None,
    ):
        """Create a coordinator with an async publication callback.

        ``clock`` supplies timezone-aware instants for diagnostic elapsed seconds.
        It defaults to the current UTC time. Injected clocks must remain aware.
        ``explain`` supplies module-owned diagnostic text without changing admission.
        ``record_calculation`` is awaited once per attempted round in the finally
        block after publication disposition, as ``(snapshot, outcome, started,
        finished, reason)``. The timestamps are aware instants. ``outcome`` is
        None if calculation did not return. ``reason`` is ``published``, an
        outcome or supersession reason, or ``planning_error``. Callback exceptions
        are logged and contained. A slow callback delays the next round, so the
        callback must enqueue optional persistence rather than wait for storage.
        """
        self.service, self.publish = service, publish
        self.clock = clock or (lambda: datetime.now(UTC))
        self.explain = explain or explain_planning_problem
        self.record_calculation = record_calculation
        self.events = None
        self.latest = None
        self.active = None
        self.published = None
        self.task = None
        self.closed = False
        self.status, self.reason = "not_ready", None
        self.blocked_since = None
        self.block_reason = None

    def _set_status(self, status, reason=None):
        """Overwrite status and reason, logging changed blockers once.

        The first blocker starts an episode. Later reasons retain that start.
        Calculating keeps the episode open. Only ready logs elapsed seconds and
        clears it after successful publication. Diagnostic state is not persisted.
        """
        now = self.clock()
        if status in {"not_ready", "failed"} and reason:
            if reason != self.block_reason:
                previous = self.block_reason
                self.blocked_since = self.blocked_since or now
                text = self.explain(reason)
                _LOGGER.info(
                    "Planning blocked: %s (previous: %s). %s Action: %s",
                    reason,
                    previous or "none",
                    text.message,
                    text.action_label,
                )
                self.block_reason = reason
        elif status == "ready" and self.block_reason:
            _LOGGER.info(
                "Planning resumed after %.1f seconds. Previous blocker: %s",
                max(0, (now - self.blocked_since).total_seconds()),
                self.block_reason,
            )
            self.block_reason = self.blocked_since = None
        self.status, self.reason = status, reason
        if self.events and status != "calculating":
            from .activities.events import Health

            health = (
                Health.AVAILABLE
                if status == "ready"
                else Health.UNAVAILABLE
                if status == "not_ready" and reason
                else Health.DEGRADED
                if status == "failed"
                else Health.UNKNOWN
            )
            detail = (
                self.explain(reason).message
                if reason and status in {"not_ready", "failed"}
                else "A validated plan is available."
                if status == "ready"
                else "Waiting for a validated plan. Existing commands retain their original validity."
            )
            self.events.availability("planning", health, detail)

    def submit(self, snapshot: InputSnapshot):
        """Replace the latest requested snapshot and start a worker task only when needed."""
        if self.closed:
            raise RuntimeError("Planning coordinator is closed")
        self.latest = snapshot
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def invalidate(self, reason):
        """Revoke the current generation and keep the supplied not-ready reason authoritative."""
        self.latest = None
        self.published = None
        self._set_status("not_ready", reason)

    def publication_rejection(self, document, now, current):
        """Reject only hard changes, expired inputs or battery dispatch no longer supported by measured storage."""
        original = self.active
        if original is None or current is None or self.closed:
            return "inputs_unavailable"
        if (
            original.config_revision != current.config_revision
            or original.strategy != current.strategy
            or original.appliance_revision != current.appliance_revision
            or original.ev_revision != current.ev_revision
            or original.thermal_revision != current.thermal_revision
            or type(original.problem) is not type(current.problem)
        ):
            return "configuration_changed"
        if document["input_snapshot_id"] != original.snapshot_id:
            return "candidate_input_mismatch"
        if not original.current_at(now) or not current.current_at(now):
            return "publication_inputs_expired"
        if not battery_dispatch_available(document, original, current):
            return "battery_dispatch_unavailable"
        if any(
            not work.initially_heating and work.ready_at <= now
            for work in getattr(original.problem, "thermal_work", ())
        ):
            return "thermal_dispatch_window_elapsed"
        original_ev = getattr(original.problem, "ev_work", ())
        current_ev = {work.session_id: work for work in getattr(current.problem, "ev_work", ())}
        for work in original_ev:
            fresh = current_ev.get(work.session_id)
            if fresh is None:
                return "ev_session_unavailable"
            charge = sum(
                row["stored_kwh"]
                for flow in document["expected_flows"]
                for row in flow.get("ev", [])
                if row["session_id"] == work.session_id
            )
            if (
                charge
                > fresh.capacity_kwh * (FULL_SOC_PERCENT - fresh.initial_soc) / FULL_SOC_PERCENT
                + DEFAULT_VALIDATION_TOLERANCE
            ):
                return "ev_capacity_unavailable"
        return None

    async def _run(self):
        """Publish feasible active results despite soft updates. Discard hard-invalid candidates and keep newer readiness."""
        while not self.closed and self.latest is not None:
            snapshot = self.latest
            self.active = snapshot
            self._set_status("calculating")
            started = monotonic()
            calculation_started = self.clock()
            outcome = None
            calculation_reason = "planning_error"
            superseded = False
            activity = None
            _LOGGER.debug(
                "Planning calculation started. Snapshot: %s. Strategy: %s. Intervals: %d.",
                snapshot.snapshot_id,
                snapshot.strategy,
                len(snapshot.problem.intervals),
            )
            try:
                if self.events:
                    intervals = snapshot.problem.intervals
                    context = (
                        {
                            "start": intervals[0].start.isoformat(),
                            "end": (
                                intervals[-1].start + timedelta(hours=intervals[-1].duration_h)
                            ).isoformat(),
                        }
                        if intervals
                        else None
                    )
                    activity = await self.events.start(
                        "planning", "planning", "Calculate energy plan", context=context
                    )
                outcome = await self.service.calculate(snapshot)
                calculation_reason = outcome.reason
                if self.closed:
                    return
                if outcome.document is not None:
                    reason = self.publication_rejection(
                        outcome.document, self.service.clock(), self.latest
                    )
                    if reason:
                        raise PublicationSuperseded(reason)
                    await self.publish(outcome.document)
                    self.published = (snapshot, outcome.document)
                    calculation_reason = "published"
                    _LOGGER.info(
                        "Plan published in %.2f seconds. Strategy: %s. Validated forecast intervals: %d.",
                        monotonic() - started,
                        snapshot.strategy,
                        len(snapshot.problem.intervals),
                    )
                    _LOGGER.debug(
                        "Published plan %s from input snapshot %s. Valid until %s.",
                        outcome.document.get("plan_id"),
                        snapshot.snapshot_id,
                        outcome.document.get("valid_until"),
                    )
                    if self.closed or self.latest is None:
                        continue
                    self._set_status("ready", outcome.reason)
                else:
                    if snapshot is not self.latest:
                        continue
                    self._set_status("failed", outcome.reason or "no_usable_plan")
            except PublicationSuperseded as error:
                superseded = True
                calculation_reason = str(error) or "inputs_changed"
                _LOGGER.info(
                    "Plan discarded before publication (%s). No partial plan was activated. %s",
                    str(error) or "inputs_changed",
                    "Rechecking the latest inputs."
                    if snapshot is not self.latest and self.latest is not None
                    else "Waiting for the next valid input refresh.",
                )
                if snapshot is self.latest:
                    self.invalidate(str(error) or "publication_inputs_expired")
            except asyncio.CancelledError:
                calculation_reason = "interrupted"
                raise
            except Exception:
                calculation_reason = "planning_error"
                _LOGGER.exception("Planning round failed. No partial plan published")
                if not self.closed and snapshot is self.latest:
                    self._set_status("failed", "planning_error")
            finally:
                if activity:
                    from .activities.events import Outcome

                    published = calculation_reason == "published"
                    if published:
                        state = Outcome.COMPLETED
                    elif calculation_reason == "interrupted":
                        state = Outcome.INTERRUPTED
                    elif self.closed:
                        state = Outcome.CANCELLED
                    elif superseded:
                        state = Outcome.SKIPPED
                    else:
                        state = Outcome.FAILED
                    await self.events.finish(
                        activity,
                        state,
                        "A validated plan was published."
                        if published
                        else "No plan was activated. Existing commands retain their original validity.",
                        usable=published,
                        context={
                            "plan_id": outcome.document.get("plan_id"),
                            "plan_generated_at": outcome.document.get("generated_at"),
                            "plan_strategy": outcome.document.get("strategy"),
                        }
                        if published
                        else None,
                    )
                if self.record_calculation is not None:
                    try:
                        await self.record_calculation(
                            snapshot, outcome, calculation_started, self.clock(), calculation_reason
                        )
                    except Exception:
                        _LOGGER.exception("Calculation history unavailable. Planning is unaffected")
            if snapshot is self.latest:
                return

    async def wait(self):
        """Wait for the current coordinator task when a round has been started."""
        if self.task:
            await self.task

    async def close(self):
        """Prevent further submission and await completion without publishing a closed result."""
        self.closed = True
        await self.wait()
