"""Coordinate selected observations and independent future inputs before planning."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from open_energy_planner.core.units import (
    FULL_SOC_PERCENT,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    WATTS_PER_KW,
)
from open_energy_planner.home_assistant.observations import (
    BasisSource,
    InputsUnavailable,
    SnapshotAssembler,
    SourceConfiguration,
)

from .optimization.domain import Strategy
from .planning import PlanningCoordinator
from .planning_policy import FINE_INTERVAL_MINUTES

MIN_DEVIATION_REPLAN_SECONDS = 300
GRID_DEVIATION_W = 1000
BATTERY_DEVIATION_FRACTION = 0.05
THERMAL_DEVIATION_C = 2

_LOGGER = logging.getLogger(__name__)


class ObservationState(Protocol):
    """Define persistence operations required for current-input invalidation and source changes."""

    def invalidate_plan(self) -> None:
        """Revoke the active plan pointer while retaining immutable archived plans."""
        ...

    def store_sources(self, configuration: SourceConfiguration) -> None:
        """Atomically persist source replacement and invalidate the active plan pointer."""
        ...


class LiveInputs:
    """In-memory current observations. Archived plans retain exact input evidence."""

    def __init__(
        self,
        planner: PlanningCoordinator,
        repository: ObservationState,
        basis_source: BasisSource | None = None,
        clock=None,
    ):
        """Coordinate current reports with a nonblocking future-input source and optional test clock."""
        self.planner, self.repository, self.basis_source = planner, repository, basis_source
        self.clock = clock or (lambda: datetime.now(UTC))
        self.assembler = SnapshotAssembler()
        self.configuration = None
        self.report = None
        self.current_snapshot = None
        self.last_submission = None
        self.last_basis = None
        self.last_quarter = None
        self.configuration_issue = lambda: None
        self.requested_strategy = lambda: Strategy.COST
        self.strategy_issue = lambda: None
        self.prepare_snapshot = lambda snapshot, now: snapshot

    def invalidate(self, reason):
        """Revoke the current planning generation and persisted publication with a readiness reason."""
        self.current_snapshot = None
        self.planner.invalidate(reason)
        self.repository.invalidate_plan()

    def configure(self, configuration):
        # Do not install an unacknowledged revision or invalidate a still-valid plan
        # when the adapter cannot commit the complete replacement.
        """Persist source replacement before installing its revision and clearing old observations."""
        self.repository.store_sources(configuration)
        self.install_configuration(configuration)

    def install_configuration(self, configuration):
        """Install roles derived from already durable settings, without a second competing settings write."""
        self.configuration = configuration
        self.report = None
        self.current_snapshot = None
        self.last_submission = None
        self.last_basis = self.last_quarter = None
        self.planner.invalidate("waiting_for_measurements")
        self.repository.invalidate_plan()
        _LOGGER.info(
            "Measurement configuration loaded (%d selected inputs). Waiting for a fresh Home Assistant capture before planning.",
            len(configuration.bindings),
        )

    def receive(self, report):
        """Accept a strictly newer capture and refresh readiness. Reject reordered reports."""
        self._accept_report(report)
        self.refresh()

    async def receive_async(self, report):
        """Accept a capture under the caller-held state lock and prepare it off-loop."""
        self._accept_report(report)
        await self.refresh_async()

    def _accept_report(self, report):
        """Reject reordered reports before replacing the current capture."""
        if self.report is not None and report.captured_at <= self.report.captured_at:
            raise ValueError("Observation reports must advance capture time")
        first_capture = self.report is None
        self.report = report
        if first_capture:
            _LOGGER.info(
                "Home Assistant observation stream received (%d readings). Checking planning prerequisites.",
                len(report.readings),
            )

    def refresh(self, *, force=False):
        """Validate every capture, but solve on quarters or throttled material changes.

        Explicit configuration/resume requests and readiness recovery may start
        immediately. Ordinary source versions and deviations never bypass the
        five-minute limit. Measurement freshness is validated, not a solve timer.
        """
        self._install(self._prepare(), force=force)

    async def refresh_async(self, *, force=False):
        """Prepare off-loop, draining cancellation before releasing the caller-held state lock.

        Coordinator mutation and submission remain on the event loop. The state
        lock protects the domain services borrowed by the preparation worker.
        """
        worker = asyncio.create_task(asyncio.to_thread(self._prepare))
        cancellation = None
        while True:
            try:
                prepared = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as error:
                if worker.cancelled():
                    raise
                cancellation = error
        if not self.planner.closed:
            self._install(prepared, force=force)
        if cancellation is not None:
            raise cancellation

    def _prepare(self):
        """Build inputs without mutating the event-loop-owned coordinator.

        ``refresh_async`` runs this on a worker. Its caller retains the app state
        lock because the basis source and appliance preparation borrow mutable
        domain services, including their readiness issues.

        Returns:
            An InputsUnavailable value when prerequisites are missing or expired.
            Otherwise the (now, basis, snapshot) tuple consumed by _install.
        """
        now = self.clock()
        try:
            issue = self.strategy_issue() or self.configuration_issue()
            if issue:
                raise InputsUnavailable(issue)
            if self.configuration is None:
                raise InputsUnavailable("sources_not_configured")
            if self.report is None:
                raise InputsUnavailable("waiting_for_measurements")
            self.assembler.expiry(self.configuration, self.report, now)
            basis = (
                self.basis_source.current(self.configuration, now, report=self.report)
                if self.basis_source
                else None
            )
            snapshot = self.assembler.build(self.configuration, self.report, basis, now)
            snapshot = replace(snapshot, strategy=self.requested_strategy())
            snapshot = self.prepare_snapshot(snapshot, now)
        except InputsUnavailable as error:
            return error
        return now, basis, snapshot

    def _install(self, prepared, *, force):
        """Recheck freshness and install completed preparation on the coordinator's thread."""
        if isinstance(prepared, InputsUnavailable):
            self.invalidate(str(prepared))
            self.last_submission = None
            return
        now, basis, snapshot = prepared
        try:
            self.assembler.expiry(self.configuration, self.report, self.clock())
            if self.clock() >= basis.valid_until:
                raise InputsUnavailable("forecast_or_prices_invalid")
        except InputsUnavailable as error:
            self.invalidate(str(error))
            self.last_submission = None
            return
        versions = (
            basis.config_revision,
            basis.forecast_version,
            basis.price_version,
            snapshot.appliance_revision,
            snapshot.ev_revision,
            snapshot.thermal_revision,
        )
        self.current_snapshot = snapshot
        quarter = int(now.timestamp()) // (FINE_INTERVAL_MINUTES * SECONDS_PER_MINUTE)
        elapsed = (now - self.last_submission).total_seconds() if self.last_submission else None
        if (
            force
            or self.last_submission is None
            or quarter != self.last_quarter
            or (
                elapsed >= MIN_DEVIATION_REPLAN_SECONDS
                and (versions != self.last_basis or self.deviated(snapshot, now))
            )
        ):
            changed = (
                ", ".join(
                    label
                    for label, previous, current in zip(
                        (
                            "configuration",
                            "demand/solar forecasts",
                            "market prices",
                            "appliance requests",
                            "EV charging",
                            "thermal storage",
                        ),
                        self.last_basis,
                        versions,
                        strict=True,
                    )
                    if previous != current
                )
                if self.last_basis is not None
                else ""
            )
            reason = (
                "an explicit settings or user request"
                if force
                else "all required inputs are ready"
                if self.last_submission is None
                else "a new quarter-hour started"
                if quarter != self.last_quarter
                else f"updated {changed}"
                if versions != self.last_basis
                else "measurements differ materially from the current plan"
            )
            _LOGGER.info(
                "Planning requested: %s. %s",
                reason,
                f"Previous request was {elapsed:.0f} seconds ago."
                if elapsed is not None
                else "This is the first ready request.",
            )
            _LOGGER.debug(
                "Planning input snapshot %s. Measurement version: %s. Future input versions: %s.",
                snapshot.snapshot_id,
                snapshot.measurement_version,
                versions,
            )
            self.last_submission, self.last_basis, self.last_quarter = now, versions, quarter
            self.planner.submit(snapshot)

    def deviated(self, snapshot, now):
        """Compare measured grid power and battery stock with the published trajectory.

        A 1 kW instantaneous grid difference or 5 percentage points of battery
        capacity requests a throttled round. Expected charging/discharging is
        interpolated, so normal SoC movement does not count as forecast error.
        """
        published = getattr(self.planner, "published", None)
        if not published:
            return False
        original, document = published
        problem = snapshot.problem
        batteries = {
            battery_item.device_id: battery_item.battery for battery_item in problem.batteries
        }
        before = {
            battery_item.device_id: battery_item.battery.initial_kwh
            for battery_item in original.problem.batteries
        }
        vehicles = {work.session_id: work for work in problem.ev_work}
        vehicle_before = {
            work.session_id: work.initial_soc * work.capacity_kwh / FULL_SOC_PERCENT
            for work in original.problem.ev_work
        }
        thermal = {work.device_id: work for work in problem.thermal_work}
        thermal_before = {work.device_id: work.initial_c for work in original.problem.thermal_work}
        for interval, flow in zip(
            original.problem.intervals, document["expected_flows"], strict=True
        ):
            end = interval.start + timedelta(hours=interval.duration_h)
            ends = {
                battery_item["device_id"]: battery_item["end_kwh"]
                for battery_item in flow["batteries"]
            }
            if interval.start <= now < end:
                fraction = (now - interval.start).total_seconds() / (
                    interval.duration_h * SECONDS_PER_HOUR
                )
                for key, battery in batteries.items():
                    if key not in before or key not in ends:
                        return True
                    expected = before[key] + fraction * (ends[key] - before[key])
                    if (
                        abs(battery.initial_kwh - expected)
                        >= BATTERY_DEVIATION_FRACTION * battery.capacity_kwh
                    ):
                        return True
                for row in flow.get("ev", ()):
                    identity = row["session_id"]
                    vehicle = vehicles.get(identity)
                    if vehicle is not None:
                        expected = vehicle_before[identity] + fraction * row["stored_kwh"]
                        actual = vehicle.initial_soc * vehicle.capacity_kwh / FULL_SOC_PERCENT
                        if (
                            abs(actual - expected)
                            >= BATTERY_DEVIATION_FRACTION * vehicle.capacity_kwh
                        ):
                            return True
                for row in flow.get("thermal", ()):
                    identity = row["device_id"]
                    if identity in thermal and identity in thermal_before:
                        expected = thermal_before[identity] + fraction * (
                            row["end_c"] - thermal_before[identity]
                        )
                        if abs(thermal[identity].initial_c - expected) >= THERMAL_DEVIATION_C:
                            return True
                grid_readings = [
                    reading_item.value
                    for reading_item in self.report.readings
                    if reading_item.key == "grid.power"
                    or reading_item.key.startswith("grid.power.")
                ]
                imported = flow.get(
                    "grid_import", flow.get("grid_to_load", 0) + flow.get("grid_to_battery", 0)
                )
                exported = flow.get(
                    "grid_export", flow.get("pv_to_grid", 0) + flow.get("battery_to_grid", 0)
                )
                expected_w = (imported - exported) / interval.duration_h * WATTS_PER_KW
                return (
                    bool(grid_readings) and abs(sum(grid_readings) - expected_w) >= GRID_DEVIATION_W
                )
            before = ends
            for row in flow.get("ev", ()):
                vehicle_before[row["session_id"]] += row["stored_kwh"]
            for row in flow.get("thermal", ()):
                thermal_before[row["device_id"]] = row["end_c"]
        return False
