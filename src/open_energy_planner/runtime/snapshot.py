"""Assemble the shared HTTP snapshot for app reads and installation save responses."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aiohttp import web

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.service import ApplianceView
from open_energy_planner.appliances.forecasting import ConsumerView
from open_energy_planner.build_info import RELEASE
from open_energy_planner.home_assistant.market_prices import price_request
from open_energy_planner.home_assistant.observation_wire import (
    configuration_document,
    observation_status,
)
from open_energy_planner.home_assistant.source_choices import configured_sources
from open_energy_planner.installation.api import catalogue as installation_catalogue
from open_energy_planner.installation.api import warnings as installation_warnings
from open_energy_planner.installation.commands import execution_document
from open_energy_planner.installation.storage import document as installation_document
from open_energy_planner.runtime.diagnostics.http import phase, timed_lock
from open_energy_planner.solar.runtime import learning_document
from open_energy_planner.storage.writes import complete_write


@dataclass(frozen=True, eq=False)
class SnapshotProjection:
    """Own bound read inputs for one capture, with no references to mutable services."""

    document: dict
    demand_document: Callable[[], dict]
    consumers: ConsumerView
    appliances: ApplianceView
    solar_learning: dict
    plan: dict | None
    enabled: bool
    execution_only: bool
    captured_at: datetime

    def project(self):
        """Render forecast series, appliance evidence and plan commands off the event loop."""
        plan = self.plan
        if self.execution_only:
            plan = execution_document(plan) if self.valid_plan_at(self.captured_at) else None
        elif plan:
            plan = {
                key: value
                for key, value in plan.items()
                if key
                not in {
                    "reference_forecast",
                    "reference_forecast_issue",
                    "comparison_bindings",
                    "comparison_names",
                }
            }
        return {
            **self.document,
            "plan": plan,
            "plan_invalidated": self.document["plan_invalidated"]
            or (self.execution_only and self.plan is not None and plan is None),
            "demand": self.demand_document(),
            "consumers": self.consumers.document(now=self.captured_at),
            "solar": {**self.document["solar"], "learning": learning_document(self.solar_learning)},
            "appliances": self.appliances.document(self.captured_at, self.enabled, plan),
        }

    def valid_plan_at(self, now):
        """Use the published command deadline without mutating an archived plan."""
        return self.plan is not None and now < instant(self.plan["valid_until"])

    def can_respond(self, current_state, current_appliances, state_locked, now, document):
        """Reject superseded plans or start requests after asynchronous preparation."""
        if self.plan is not current_state["plan"] or self.enabled != current_state["enabled"]:
            return False
        if (
            self.execution_only
            and self.valid_plan_at(self.captured_at)
            and not self.valid_plan_at(now)
        ):
            return False
        return all(
            not state_locked
            and self.appliances.same_inputs(current_appliances)
            and self.valid_plan_at(now)
            and now < instant(row["request"]["expires_at"])
            for row in document["appliances"]["devices"]
            if row["request"]
        )


def create_snapshot_handler(
    *,
    lock,
    now,
    source,
    repository,
    settlement_request,
    savings_runtime,
    live,
    current_prices,
    contract_api,
    bonus_counters,
    forecast_history_request,
    demand,
    appliances,
    ev,
    solar,
    thermal,
    installation,
    installation_basis,
    setup_catalogue,
    synthetic_batteries,
    planner,
):
    """Bind the snapshot to app-owned services and return its shared request handler.

    Sample the clock and the active price runtime on each use. Keep settlement
    outside the app lock. Refresh and capture state under it before projecting and encoding.
    """

    async def snapshot(request):
        """Capture settlement inputs under the state lock and await their archive read outside it.

        Reacquire the lock to refresh live validity and assemble current state, so
        intervening controls are reflected rather than overwritten by captured state.
        """
        async with timed_lock(request, "snapshot_capture_wait", lock):
            pending_settlement = settlement_request()
        with phase(request, "snapshot_settlement"):
            settlement = await pending_settlement
        async with timed_lock(request, "snapshot_state_wait", lock):
            with phase(request, "snapshot_appliance_tick"):
                cancellation = await complete_write(
                    appliances.tick,
                    now(),
                    repository.state["enabled"],
                    repository.state["plan"],
                )
                if cancellation is not None:
                    raise cancellation
            if live:
                with phase(request, "snapshot_live_refresh"):
                    await live.refresh_async()
            projection = capture_projection(request, settlement, now())
        while True:
            with phase(request, "snapshot_document"):
                document = await asyncio.to_thread(projection.project)
            with phase(request, "snapshot_json"):
                response = await asyncio.to_thread(web.json_response, document)
            if projection.can_respond(
                repository.state, lock.settings.appliances, lock.locked(), now(), document
            ):
                return response
            async with timed_lock(request, "snapshot_state_wait", lock):
                projection = capture_projection(request, settlement, now())

    def capture_projection(request, settlement, now):
        """Capture bounded state on its owner and borrow only immutable/replaced values."""
        with phase(request, "snapshot_bonus_history"):
            pending_history = contract_api.history_request()
        with phase(request, "snapshot_forecast_history"):
            quality_request = forecast_history_request(now) if source == "home_assistant" else None
        with phase(request, "snapshot_demand"):
            demand_document = demand.capture_document(now, include_consumer_request=True)
        with phase(request, "snapshot_projection_capture"):
            devices = installation_catalogue(
                installation, solar, live.current_snapshot if live else None
            )
            if installation_basis or installation.current is not None:
                devices = setup_catalogue.devices(devices)
            if devices is None:
                devices = (
                    []
                    if live
                    else [
                        {
                            "id": unit.device_id,
                            "kind": "battery",
                            "name": "Battery",
                            "configured": True,
                            "can_follow_grid": unit.can_follow_grid,
                        }
                        for unit in synthetic_batteries
                    ]
                )
            document = {
                "release": RELEASE,
                "settlement_request": settlement,
                "savings_status": {
                    "issue": savings_runtime.issue,
                    "action_required": savings_runtime.attention,
                },
                **repository.state,
                "source": source,
                "market_price_request": price_request(current_prices(), now)
                if source == "home_assistant"
                else None,
                "forecast_quality_request": quality_request,
                "ev": ev.document(
                    now,
                    (setup_catalogue.value or {}).get("timezone", "UTC"),
                    setup_catalogue.value,
                    repository.state["plan"],
                ),
                "solar": solar.document(now, include_learning=False),
                "thermal": thermal.document(now, setup_catalogue.value, repository.state["plan"]),
                "installation": {
                    "configuration": installation_document(installation.current),
                    "issue": installation.issue,
                    "devices": devices,
                    "warnings": installation_warnings(installation, solar),
                    "setup_discovery_requested": setup_catalogue.refresh_status()["id"],
                    "setup_source_entities": configured_sources(
                        live.configuration if live else None, ev.state, thermal.state
                    ),
                    "source_choices_requested": setup_catalogue.choices.pending(now),
                },
                "bonus_history": {
                    "request": {
                        **pending_history.document(),
                        "version": pending_history.version,
                    }
                    if pending_history
                    else None,
                    "issue": bonus_counters.issue
                    or (contract_api.history_issue if pending_history else None),
                },
                "config_revision": live.configuration.revision
                if live and live.configuration
                else "demo-1",
                "measurement_config": configuration_document(live.configuration)
                if live and live.configuration
                else None,
                "observations": observation_status(live.configuration, live.report)
                if live
                else None,
                "plan_invalidated": bool(live and repository.state["plan"] is None),
                "planning": {
                    "status": planner.status,
                    "reason": planner.reason,
                    "requested_snapshot_id": planner.latest.snapshot_id if planner.latest else None,
                },
            }
        return SnapshotProjection(
            document,
            demand_document,
            demand.consumers.read_view(),
            appliances.read_view(),
            dict(solar.learning),
            repository.state["plan"],
            repository.state["enabled"],
            request.headers.get("X-OEP-View") == "commands",
            now,
        )

    return snapshot
