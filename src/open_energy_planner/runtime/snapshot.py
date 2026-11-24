"""Assemble the shared HTTP snapshot for app reads and installation save responses."""

from aiohttp import web

from open_energy_planner.build_info import RELEASE
from open_energy_planner.home_assistant.market_prices import price_request
from open_energy_planner.home_assistant.observation_wire import (
    configuration_document,
    observation_status,
)
from open_energy_planner.home_assistant.source_choices import configured_sources
from open_energy_planner.installation.api import catalogue as installation_catalogue
from open_energy_planner.installation.api import warnings as installation_warnings
from open_energy_planner.installation.storage import document as installation_document
from open_energy_planner.runtime.diagnostics.http import phase, timed_lock
from open_energy_planner.storage.writes import complete_write

HA_GRID_FIELDS = (
    "grid_power_entity",
    "grid_power_inverted",
    "grid_export_entity",
    "import_limit_w",
    "export_limit_w",
)


def device_catalogue(installation, solar, setup, installation_basis, synthetic_batteries, live):
    """Resolve configuration, Energy membership and synthetic capabilities consistently."""
    devices = installation_catalogue(installation, solar, live.current_snapshot if live else None)
    if installation_basis or installation.current is not None:
        devices = setup.devices(devices)
    if devices is not None:
        return devices
    return (
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
    outside the app lock and serialize refresh, state capture and encoding under it.
    """

    async def snapshot(request, *, ha_requests=False):
        """Capture settlement inputs under the state lock and await their archive read outside it.

        Reacquire the lock to refresh live validity and assemble current state, so
        intervening controls are reflected rather than overwritten by captured state.
        """
        async with timed_lock(request, "snapshot_capture_wait", lock):
            pending_settlement = settlement_request()
        with phase(request, "snapshot_settlement"):
            settlement = await pending_settlement
        async with timed_lock(request, "snapshot_state_wait", lock):
            instant = now()
            if not ha_requests:
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
            with phase(request, "snapshot_bonus_history"):
                pending_history = contract_api.history_request()
            with phase(request, "snapshot_forecast_history"):
                quality_request = (
                    forecast_history_request(now()) if source == "home_assistant" else None
                )
            with phase(request, "snapshot_demand"):
                if ha_requests:
                    demand_state = demand.ha_history(instant)
                else:
                    demand_state = demand.document(instant)
                    demand_state["request"] = demand_state.get(
                        "request"
                    ) or demand.consumers.request(demand.configuration, instant)
            with phase(request, "snapshot_document"):
                devices = device_catalogue(
                    installation,
                    solar,
                    setup_catalogue,
                    installation_basis,
                    synthetic_batteries,
                    live,
                )
                document = {
                    "release": RELEASE,
                    "instance_id": repository.state["instance_id"],
                    "settlement_request": settlement,
                    "savings_status": {
                        "issue": savings_runtime.issue,
                        "action_required": savings_runtime.attention,
                    },
                    "source": source,
                    "market_price_request": price_request(current_prices(), now())
                    if source == "home_assistant"
                    else None,
                    "forecast_quality_request": quality_request,
                    "demand": demand_state,
                    "consumers": demand.consumers.ha_projection(instant)
                    if ha_requests
                    else demand.consumers.document(now=instant),
                    "appliances": appliances.document(
                        now(), repository.state["enabled"], repository.state["plan"]
                    ),
                    "ev": ev.document(
                        now(),
                        (setup_catalogue.value or {}).get("timezone", "UTC"),
                        setup_catalogue.value,
                        repository.state["plan"],
                    ),
                    "solar": solar.ha_history(instant) if ha_requests else solar.document(instant),
                    "thermal": thermal.document(
                        now(), setup_catalogue.value, repository.state["plan"]
                    ),
                    "installation": {
                        "configuration": {
                            name: getattr(installation.current, name) for name in HA_GRID_FIELDS
                        }
                        if ha_requests and installation.current is not None
                        else installation_document(installation.current),
                        "issue": installation.issue,
                        "devices": devices,
                        "warnings": installation_warnings(installation, solar),
                        "setup_discovery_requested": setup_catalogue.refresh_status()["id"],
                        "setup_source_entities": configured_sources(
                            live.configuration if live else None, ev.state, thermal.state
                        ),
                        "source_choices_requested": setup_catalogue.choices.pending(now()),
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
                }
                if not ha_requests:
                    plan = repository.state["plan"]
                    document = {
                        **repository.state,
                        **document,
                        "plan": {
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
                        if plan
                        else None,
                        "config_revision": live.configuration.revision
                        if live and live.configuration
                        else "demo-1",
                        "measurement_config": configuration_document(live.configuration)
                        if live and live.configuration
                        else None,
                        "observations": observation_status(live.configuration, live.report)
                        if live
                        else None,
                        "plan_invalidated": bool(live and plan is None),
                        "planning": {
                            "status": planner.status,
                            "reason": planner.reason,
                            "requested_snapshot_id": planner.latest.snapshot_id
                            if planner.latest
                            else None,
                        },
                    }
            with phase(request, "snapshot_json"):
                return web.json_response(document)

    return snapshot
