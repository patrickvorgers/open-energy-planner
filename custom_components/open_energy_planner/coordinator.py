"""One local schedule, one boundary timer and sequential HTTP synchronization."""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta

from aiohttp import ClientError, ServerFingerprintMismatch
from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .catalogue_discovery import CatalogueDiscovery
from .client import AuthenticationError, ReleaseMismatchError
from .const import DOMAIN, RELEASE
from .history import HistoryBridge
from .market_prices import MarketPriceBridge
from .observation_bridge import ObservationBridge
from .protocol import RevokedPlan, Schedule, identifier, timestamp, validate_device_configuration
from .settlement import SettlementBridge
from .solar_forecasts import SolarForecastBridge
from .wire_limits import MAX_SNAPSHOT_RESPONSE_BYTES, validate_projection_size

CATALOGUE_RETRY_MINUTES = 5
CATALOGUE_CONFIRM_SECONDS = 60
SOLAR_FORECAST_REFRESH_MINUTES = 5
POLL_INTERVAL_SECONDS = 5


_LOGGER = logging.getLogger(__name__)


class PlanCoordinator:
    """Own HA enable intent, current command projection, timers and sequential app exchange."""

    def __init__(self, hass, entry, client):
        """Initialize entry-local state, storage, listeners and the synchronization lock."""
        self.hass, self.entry, self.client = hass, entry, client
        self.schedule = Schedule()
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.listeners = []
        self.projection = self.schedule.project(dt_util.utcnow())
        self.boundary_cancel = None
        self.poll_cancel = None
        self.startup_cancel = None
        self.lock = asyncio.Lock()
        self.pending_enabled = None
        self.issue = None
        self.notification_message = None
        self.planning_status = None
        self.closed = False
        self.observation_bridge = None
        self.observations = None
        self.devices = None
        self.consumer_forecasts = {}
        self.appliances = {}
        self.appliance_history_attempts = {}
        self.appliance_history_task = None
        self.ev = {}
        self.ev_sessions = {}
        self.ev_watch = ()
        self.ev_cancel = None
        self.thermal = {}
        self.thermal_watch = ()
        self.thermal_cancel = None
        self.grid_configuration = None
        self.catalogue_sent = None
        self.catalogue_request = None
        self.catalogue_discovery = CatalogueDiscovery(hass)
        self.catalogue_context = None
        self.choice_requests = set()
        self.catalogue_failed_at = None
        self.catalogue_confirmed_at = None
        self.catalogue_bindings_rejected = False
        self.solar_forecast_sent = None
        self.solar_forecast_attempted_at = None
        self.solar_forecast_generation = None
        self.history_bridge = HistoryBridge(hass)
        self.settlement_bridge = SettlementBridge(hass)
        self.settlement_bonus_bridge = HistoryBridge(hass)
        self.demand_history_bridge = HistoryBridge(hass)
        self.forecast_history_bridge = HistoryBridge(hass)
        self.solar_history_bridge = HistoryBridge(hass)
        self.solar_forecast_bridge = SolarForecastBridge(hass)
        self.market_prices = MarketPriceBridge(hass)

    async def load(self):
        """Restore enable/resume intent without blindly restoring a previously accepted plan."""
        saved = await self.store.async_load()
        if saved:
            self.schedule.enabled = saved.get("enabled", True)
            value = saved.get("resume_after")
            self.schedule.resume_after = timestamp(value) if value else None
        # Never blindly restore a plan: fetching and validating app state comes first.
        self.refresh()

    async def save(self):
        """Persist user intent and its resume boundary, excluding plans and transient measurements."""
        await self.store.async_save(
            {
                "enabled": self.schedule.enabled,
                "resume_after": self.schedule.resume_after.isoformat()
                if self.schedule.resume_after
                else None,
            }
        )

    @callback
    def subscribe(self, listener):
        """Register a projection listener and return its lifecycle unsubscribe callback."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    @callback
    def refresh(self, now=None):
        """Project current commands and schedule their next boundary without recalculating a plan.

        All virtual entities read the same projection. Emit a command event only when
        it changes, and replace the existing boundary timer rather than accumulating timers.
        """
        if self.closed:
            return
        now = now or dt_util.utcnow()
        previous = self.projection
        self.projection = self.schedule.project(now)
        if self.projection.command and any(
            target.kind == "thermal" for target in self.projection.command.devices
        ):
            from .thermal import command as thermal_command

            targets = tuple(
                replace(target, power_w=None)
                if target.kind == "thermal" and thermal_command(self, target.device_id) is None
                else target
                for target in self.projection.command.devices
            )
            self.projection = replace(
                self.projection, command=replace(self.projection.command, devices=targets)
            )
        # All entity properties read this one immutable projection before any write.
        for listener in tuple(self.listeners):
            listener()
        if self.projection != previous:
            command = self.projection.command
            self.hass.bus.async_fire(
                f"{DOMAIN}_command",
                {
                    "entry_id": self.entry.entry_id,
                    "available": command is not None,
                    "status": self.projection.status,
                    "mode": command.mode if command else None,
                    "grid_active": command.grid_active if command else None,
                    "devices": {
                        device_item.device_id: {
                            "kind": device_item.kind,
                            "power_w": device_item.power_w,
                            "enabled": device_item.enabled,
                        }
                        for device_item in command.devices
                    }
                    if command
                    else {},
                    "plan_id": self.schedule.plan.plan_id if self.schedule.plan else None,
                },
            )
        if self.boundary_cancel:
            self.boundary_cancel()
            self.boundary_cancel = None
        boundary = self.schedule.next_boundary(now)
        if boundary:
            self.boundary_cancel = async_track_point_in_utc_time(self.hass, self.refresh, boundary)
        self.watch_thermal()

    @callback
    def watch_thermal(self):
        """Refresh local heater guards on source changes. Command deadlines remain independent."""
        sources = tuple(
            sorted(
                {
                    device[field]
                    for device in self.thermal.get("devices", [])
                    for field in ("temperature_entity", "power_entity")
                }
            )
        )
        if sources != self.thermal_watch:
            if self.thermal_cancel:
                self.thermal_cancel()
                self.thermal_cancel = None
            self.thermal_watch = sources
            if sources:

                @callback
                def changed(_event):
                    """Withdraw unsafe permission immediately after any contributing report changes."""
                    self.refresh()

                self.thermal_cancel = async_track_state_change_event(
                    self.hass, list(sources), changed
                )

    async def set_enabled(self, value):
        """Apply explicit local intent immediately, persist it and synchronize with the app."""
        now = dt_util.utcnow()
        if value == self.schedule.enabled and self.pending_enabled is None:
            return
        if value:
            self.schedule.resume(now)
        else:
            self.schedule.pause(now)
        self.pending_enabled = value
        self.refresh()
        await self.save()
        await self.sync()

    async def sync(self, _now=None):
        """Serialize authenticated snapshots/reports and accept only compatible validated plans.

        Known local required-source faults revoke commands even during app outage. A
        connection failure alone retains only normally valid commands. Delayed replies
        cannot override newer user intent. Physical device control remains in automations.
        After persistence and plan acknowledgement, one cancellable background job
        refreshes selected appliance histories and resumes bounded hourly batches.
        It never holds this command synchronization open. Individual Recorder reads
        and uploads retain their existing timeouts. History faults remain visible
        as source-bound progress without withdrawing valid physical commands.
        """
        if self.closed:
            return
        async with self.lock:
            if self.closed:
                return
            try:
                # Known local core faults revoke commands even when the app is unreachable.
                if self.observation_bridge:
                    ready, _ = self.observation_bridge.capture(dt_util.utcnow())
                    if not ready:
                        self.schedule.invalidate()
                        self.refresh()
                intent_at_request = self.schedule.resume_after
                if self.pending_enabled is not None:
                    data = await self.client.enabled(
                        self.pending_enabled, self.schedule.resume_after.isoformat()
                    )
                    if self.schedule.resume_after != intent_at_request:
                        return
                    self.pending_enabled = None
                else:
                    data = await self.client.snapshot()
                if self.closed or self.schedule.resume_after != intent_at_request:
                    return
                if data.get("release") != RELEASE:
                    self.action_required(
                        "release_mismatch",
                        "Update the app and integration to the same release. "
                        "Existing commands remain usable only within their normal validity.",
                    )
                    return
                self.market_prices.start(data.get("market_price_request"), self.client)
                configuration = data.get("measurement_config")
                if configuration is not None:
                    bridge = ObservationBridge(self.hass.states, configuration)
                    if bridge.revision != self.schedule.config_revision:
                        self.schedule.config_revision = bridge.revision
                        self.schedule.invalidate()
                    self.observation_bridge = bridge
                    ready, report = bridge.capture(
                        dt_util.utcnow(),
                        location={
                            "latitude": self.hass.config.latitude,
                            "longitude": self.hass.config.longitude,
                            "timezone": self.hass.config.time_zone,
                        },
                    )
                    if not ready:
                        self.schedule.invalidate()
                        self.refresh()
                    data = await self.client.observations(report)
                    if self.closed or self.schedule.resume_after != intent_at_request:
                        return
                    if data.get("release") != RELEASE:
                        raise ValueError("Observation response release mismatch")
                    pending_history = data.get("bonus_history", {}).get("request")
                    if pending_history:
                        historical = await self.history_bridge.capture(pending_history)
                        if historical is not None:
                            try:
                                data = await self.client.history(historical)
                            except ClientError:
                                # Invalid/gapped history is diagnosed by the app. A fresh
                                # snapshot retains that reason and normal plan validation.
                                data = await self.client.snapshot()
                        if self.closed or self.schedule.resume_after != intent_at_request:
                            return
                        if data.get("release") != RELEASE:
                            raise ValueError("History response release mismatch")
                    pending_demand = data.get("demand", {}).get("request")
                    if pending_demand:
                        historical = await self.demand_history_bridge.capture(pending_demand)
                        if historical is not None:
                            try:
                                data = await self.client.demand_history(historical)
                            except ClientError:
                                data = await self.client.snapshot()
                        if self.closed or self.schedule.resume_after != intent_at_request:
                            return
                        if data.get("release") != RELEASE:
                            raise ValueError("Demand history response release mismatch")
                    pending_solar = data.get("solar", {}).get("request")
                    if pending_solar:
                        historical = await self.solar_history_bridge.capture(pending_solar)
                        if historical is not None:
                            try:
                                data = await self.client.request(
                                    "POST", "/v1/solar/history", historical
                                )
                            except ClientError:
                                data = await self.client.snapshot()
                        if self.closed or self.schedule.resume_after != intent_at_request:
                            return
                        if data.get("release") != RELEASE:
                            raise ValueError("Solar history response release mismatch")
                else:
                    revision = identifier(data.get("config_revision"))
                    if (
                        self.observation_bridge is not None
                        or revision != self.schedule.config_revision
                    ):
                        self.schedule.invalidate()
                    self.observation_bridge = None
                    self.schedule.config_revision = revision
                enabled = data.get("enabled")
                if data.get("config_revision") != self.schedule.config_revision:
                    # A changed revision invalidates commands even if this reply is rejected.
                    self.schedule.invalidate()
                    raise ValueError("Installation configuration changed during synchronization")
                if type(enabled) is not bool:
                    raise ValueError("Invalid installation snapshot")
                if data.get("plan_invalidated") is True:
                    self.schedule.invalidate()
                now = dt_util.utcnow()
                if data.get("source") == "home_assistant":
                    from .source_choices import refresh_choices

                    try:
                        self.catalogue_discovery.configure(
                            data.get("installation", {}).get("setup_source_entities", [])
                        )
                    except (ValueError, TypeError):
                        if not self.catalogue_bindings_rejected:
                            self.catalogue_discovery.repair()
                            _LOGGER.warning(
                                "Home Assistant source bindings cannot be used for metadata refresh. "
                                "Previous metadata is retained without freshness confirmation. "
                                "Check the configured source identities in settings"
                            )
                        self.catalogue_bindings_rejected = True
                    else:
                        if self.catalogue_bindings_rejected:
                            self.catalogue_failed_at = None
                            _LOGGER.info(
                                "Home Assistant source bindings accepted. Metadata refresh can resume"
                            )
                        self.catalogue_bindings_rejected = False
                    await refresh_choices(
                        self, data.get("installation", {}).get("source_choices_requested", [])
                    )
                    if self.closed:
                        return
                catalogue_request = data.get("installation", {}).get("setup_discovery_requested")
                forced_catalogue = self.catalogue_sent is None or (
                    catalogue_request and catalogue_request != self.catalogue_request
                )
                if (
                    data.get("source") == "home_assistant"
                    and not self.catalogue_bindings_rejected
                    and (
                        forced_catalogue
                        or self.catalogue_failed_at is None
                        or now - self.catalogue_failed_at
                        >= timedelta(minutes=CATALOGUE_RETRY_MINUTES)
                    )
                ):
                    from .catalogue_transfer import prepare_catalogue

                    confirmation_repair = False
                    try:
                        catalogue = await self.catalogue_discovery.capture(
                            force=bool(forced_catalogue), now=now
                        )
                        if self.closed:
                            return
                        if catalogue is not None:
                            generation = self.catalogue_discovery.captured_generation
                            prepared = await self.hass.async_add_executor_job(
                                prepare_catalogue, catalogue
                            )
                            if generation != self.catalogue_discovery.generation:
                                prepared = None
                            if prepared is not None:
                                for page in prepared.pages:
                                    if self.closed:
                                        return
                                    membership = await self.client.request(
                                        "POST", "/v1/ha/catalogue", page
                                    )
                                if membership.get("accepted") is not True:
                                    raise ValueError("Catalogue snapshot was not accepted")
                                if self.closed:
                                    return
                                self.catalogue_discovery.acknowledge(catalogue)
                                self.catalogue_context = prepared
                                self.catalogue_confirmed_at = now
                                self.solar_forecast_sent = None
                                self.solar_forecast_attempted_at = None
                                if membership.get("devices") is not None:
                                    data["installation"]["devices"] = membership["devices"]
                                if membership.get("configuration_issue"):
                                    self.schedule.invalidate()
                                    data["plan"] = None
                                    data["planning"] = {
                                        "status": "not_ready",
                                        "reason": membership["configuration_issue"],
                                    }
                        elif (
                            self.catalogue_context is not None
                            and self.catalogue_discovery.healthy
                            and (
                                self.catalogue_confirmed_at is None
                                or now - self.catalogue_confirmed_at
                                >= timedelta(seconds=CATALOGUE_CONFIRM_SECONDS)
                            )
                        ):
                            try:
                                await self.client.request(
                                    "POST",
                                    "/v1/ha/catalogue",
                                    {"confirmed_revision": self.catalogue_context.revision},
                                )
                            except (AuthenticationError, ReleaseMismatchError):
                                raise
                            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                                confirmation_repair = True
                                raise
                            self.catalogue_confirmed_at = now
                        self.catalogue_failed_at = None
                    except (AuthenticationError, ReleaseMismatchError):
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        self.catalogue_discovery.repair()
                        self.catalogue_failed_at = None if confirmation_repair else now
                        _LOGGER.debug("Setup catalogue refresh failed", exc_info=True)
                        if catalogue_request:
                            try:
                                await self.client.request(
                                    "POST",
                                    "/v1/ha/catalogue",
                                    {"refresh_failed": catalogue_request},
                                )
                            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                                _LOGGER.debug("Source refresh outcome could not be confirmed")
                    finally:
                        self.catalogue_sent, self.catalogue_request = now, catalogue_request
                if (
                    self.catalogue_context is not None
                    and self.catalogue_discovery.healthy
                    and (
                        self.solar_forecast_attempted_at is None
                        or self.solar_forecast_generation != self.solar_forecast_bridge.generation
                        or now - self.solar_forecast_attempted_at
                        >= timedelta(minutes=SOLAR_FORECAST_REFRESH_MINUTES)
                    )
                ):
                    self.solar_forecast_attempted_at = now
                    self.solar_forecast_generation = self.solar_forecast_bridge.generation
                    try:
                        generation = self.solar_forecast_bridge.generation
                        predictions = await self.solar_forecast_bridge.capture(
                            self.catalogue_context.snapshot["suggestions"].get(
                                "solar_configurations", []
                            ),
                            now,
                        )
                        if self.closed:
                            return
                        if self.catalogue_discovery.healthy:
                            await self.client.request("POST", "/v1/solar/ha-forecasts", predictions)
                            self.solar_forecast_sent = now
                            self.solar_forecast_generation = generation
                    except ReleaseMismatchError:
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        _LOGGER.debug("Solar forecast refresh failed", exc_info=True)
                # App-side disable is immediate once observed. Resuming requires a new plan.
                if not enabled and self.schedule.enabled:
                    self.schedule.pause(now)
                    # The accepted remote pause is not a newer local user action.
                    intent_at_request = self.schedule.resume_after
                elif enabled and not self.schedule.enabled:
                    # A persisted manual pause cannot be silently cancelled by a stale app.
                    self.pending_enabled = False
                self.planning_status = data.get("planning")
                if "ev" in data:
                    from .ev import capture as capture_ev
                    from .ev import receive as receive_ev
                    from .ev import watch_connections

                    self.ev = receive_ev(data["ev"])
                    watch_connections(self)
                    if self.ev["vehicles"] or self.ev["points"]:
                        self.ev = receive_ev(
                            await self.client.request(
                                "POST",
                                "/v1/ev/observations",
                                capture_ev(self, self.ev, dt_util.utcnow()),
                            )
                        )
                    if data.get("plan") and data["plan"].get("ev_context", {}).get(
                        "revision"
                    ) not in (None, self.ev["revision"]):
                        self.schedule.invalidate()
                        data["plan"] = None
                devices = data.get("installation", {}).get("devices")
                if devices is not None:
                    validate_projection_size(devices, MAX_SNAPSHOT_RESPONSE_BYTES)
                    if not isinstance(devices, list) or any(
                        not isinstance(device_item, dict)
                        or device_item.get("kind") not in ("solar", "battery")
                        for device_item in devices
                    ):
                        raise ValueError("Invalid device catalogue")
                    keys = [identifier(device_item.get("id")) for device_item in devices]
                    if len(set(keys)) != len(keys):
                        raise ValueError("Duplicate device catalogue")
                    for device in devices:
                        validate_device_configuration(device)
                self.devices = devices
                from .consumer_sensors import validate as validate_consumers

                try:
                    self.consumer_forecasts = validate_consumers(data.get("consumers", {}))
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    self.consumer_forecasts = {}
                    _LOGGER.warning(
                        "Invalid optional consumer forecasts. Continuing plan synchronization"
                    )
                try:
                    from .appliances import receive

                    receive(self, data.get("appliances", {}))
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    self.appliances = {}
                    _LOGGER.warning("Invalid optional appliance projection. Commands withdrawn")
                from .thermal import capture as capture_thermal
                from .thermal import receive as receive_thermal

                try:
                    self.thermal = receive_thermal(data.get("thermal"))
                    self.refresh()
                    if self.thermal["devices"]:
                        try:
                            thermal_reply = await self.client.request(
                                "POST",
                                "/v1/thermal/observations",
                                capture_thermal(self, self.thermal, dt_util.utcnow()),
                            )
                        except (AuthenticationError, ReleaseMismatchError):
                            raise
                        except (ClientError, TimeoutError):
                            # A transport outage retains only locally fresh, guarded permissions.
                            _LOGGER.debug("Thermal observations transfer failed", exc_info=True)
                        else:
                            if self.closed or self.schedule.resume_after != intent_at_request:
                                return
                            self.thermal = receive_thermal(thermal_reply)
                except (AuthenticationError, ReleaseMismatchError):
                    raise
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    # Preserve registry membership while revoking the execution identity.
                    self.thermal = {**self.thermal, "revision": None}
                    _LOGGER.warning("Invalid thermal projection. Heating commands withdrawn")
                physical = data.get("installation", {}).get("configuration")
                if physical is not None and not isinstance(physical, dict):
                    raise ValueError("Invalid grid configuration")
                grid = {
                    key: physical[key]
                    for key in (
                        "grid_power_entity",
                        "grid_power_inverted",
                        "grid_export_entity",
                        "import_limit_w",
                        "export_limit_w",
                    )
                    if physical is not None and key in physical
                }
                validate_device_configuration({"configuration": grid})
                self.grid_configuration = grid or None
                self.observations = data.get("observations")
                accepted = False
                if data.get("plan") is not None:
                    if devices is not None:
                        expected = {
                            device_item["id"]: device_item["kind"]
                            for device_item in devices
                            if device_item.get("configured", True)
                            and (
                                device_item["kind"] == "battery"
                                or device_item.get("can_curtail", True)
                            )
                        }
                        ev_points = {point["id"]: point for point in self.ev.get("points", [])}
                        for work in data["plan"].get("ev_context", {}).get("work", []):
                            point = ev_points.get(work["point_id"])
                            if not point or point["control"] == "read_only":
                                raise ValueError("Plan uses an unconfigured charge point")
                            expected[work["point_id"]] = "ev"
                        # The immutable plan owns thermal membership. Protocol parsing
                        # validates its complete context. Optional metadata faults or
                        # changed bindings suppress only each heater's local guard.
                        for heater in data["plan"].get("thermal_context", {}).get("devices", []):
                            expected[heater["id"]] = "thermal"
                        followers = {
                            device_item["id"]
                            for device_item in devices
                            if device_item.get("can_follow_grid") is True
                        }
                        if data["plan"].get("device_commands") is not True or any(
                            {
                                key: value.get("kind")
                                for key, value in step.get("devices", {}).items()
                            }
                            != expected
                            for step in data["plan"].get("steps", [])
                        ):
                            self.schedule.invalidate()
                            raise ValueError("Plan does not match configured device capabilities")
                        if any(
                            value.get("mode") == "follow_grid_target" and key not in followers
                            for step in data["plan"].get("steps", [])
                            for key, value in step.get("devices", {}).items()
                        ):
                            self.schedule.invalidate()
                            raise ValueError("Plan uses an unconfigured grid-following capability")
                    try:
                        accepted = self.schedule.accept(data["plan"], now)
                    except RevokedPlan:
                        # Expected during recovery: keep commands revoked, but process diagnostics.
                        pass
                await self.save()
                if self.schedule.plan and (
                    accepted or data.get("acknowledged_plan") != self.schedule.plan.plan_id
                ):
                    await self.client.acknowledge(self.schedule.plan.plan_id)
                issues = self.observations.get("issues", []) if self.observations else []
                required_sources = (
                    {
                        (binding.key, binding.entity_id)
                        for binding in self.observation_bridge.bindings
                        if binding.required
                    }
                    if self.observation_bridge
                    else set()
                )
                actionable = [
                    issue
                    for issue in issues
                    if (issue.get("key"), issue.get("entity_id")) in required_sources
                    and issue.get("reason")
                    in (
                        "missing_entity",
                        "unsupported_unit",
                        "invalid_timestamp",
                        "invalid_soc",
                    )
                ]
                if data.get("installation", {}).get("issue"):
                    self.action_required(
                        "installation_configuration_unreadable",
                        "Restore the installation settings backup or explicitly save corrected settings. No physical limits are guessed.",
                    )
                elif (self.planning_status or {}).get("reason") == "energy_configuration_changed":
                    self.action_required(
                        "energy_configuration_changed",
                        "The Home Assistant Energy installation needs configuration. Complete its Energy dashboard and open the OEP app to confirm missing device ratings and measurement bindings. Historical plans are retained.",
                    )
                elif actionable:
                    names = ", ".join(issue["entity_id"] for issue in actionable)
                    self.action_required(
                        "measurement_sources_unusable",
                        "Check the selected measurement sources, units and timestamps: " + names,
                    )
                elif data.get("bonus_history", {}).get("issue") == "bonus_counter_cache_unreadable":
                    self.action_required(
                        "bonus_counter_cache_unreadable",
                        "The saved bonus counter cannot be read. Restore its backup or "
                        "explicitly confirm a recovered manual baseline in the app. "
                        "Automatic history initialization is blocked to preserve saved evidence.",
                    )
                elif data.get("demand", {}).get("issue"):
                    demand = data["demand"]
                    if demand["issue"] == "demand_boundary_inconsistent":
                        configuration = demand.get("configuration") or {}
                        names = ", ".join(
                            sorted(
                                {
                                    term["entity_id"]
                                    for term in (
                                        *configuration.get("terms", []),
                                        *configuration.get("exclusions", []),
                                    )
                                }
                            )
                        )
                        explanation = (
                            "Household energy history contains negative balances beyond the "
                            "measurement allowance. Those hours are excluded from forecast "
                            "training. Check the Home Assistant Energy balance for overlapping "
                            "sources and separately measured devices"
                            + (": " + names if names else "")
                            + ". Usable hours, when available, are retained. When too little history "
                            "remains, forecasting uses the annual-consumption startup estimate. "
                            "Recorder data is unchanged. A backup restore is not required."
                        )
                    elif demand["issue"] == "demand_configuration_unreadable":
                        explanation = (
                            "The saved household energy configuration cannot be read. "
                            "Restore its settings backup or explicitly save corrected household "
                            "settings in the app. Historical measurements are retained."
                        )
                    else:
                        explanation = (
                            "The household forecast could not be updated. Open Forecast quality "
                            "in the app and check the app logs. The update retries automatically. "
                            "Do not restore or remove measured history to resolve a forecast error."
                        )
                    self.action_required(
                        "demand_boundary_inconsistent"
                        if demand["issue"] == "demand_boundary_inconsistent"
                        else "demand_forecast_unusable",
                        explanation,
                    )
                elif data.get("demand", {}).get("history_issues"):
                    names = ", ".join(sorted(data["demand"]["history_issues"]))
                    self.action_required(
                        "demand_history_unavailable",
                        "Household energy history cannot be read from Recorder for: "
                        + names
                        + ". Check Recorder and select cumulative energy sensors with "
                        "long-term statistics. The available forecast remains active. "
                        "Without usable history the startup estimate is used.",
                    )
                elif data.get("solar", {}).get("issue") == "solar_configuration_unreadable":
                    self.action_required(
                        "solar_configuration_unreadable",
                        "Restore the saved solar configuration or explicitly save corrected settings in the app.",
                    )
                elif solar_history_issues := {
                    entity: issue
                    for entity, issue in data.get("solar", {}).get("history_issues", {}).items()
                    if issue
                    in {
                        "unsupported_statistics",
                        "recorder_unavailable",
                        "history_continuity_unavailable",
                        "history_cache_unreadable",
                    }
                }:
                    names = ", ".join(sorted(solar_history_issues))
                    self.action_required(
                        "solar_history_unavailable",
                        "Check Recorder and the selected cumulative solar production sensors: "
                        + names
                        + ". The physical startup forecast remains available when weather is usable.",
                    )
                elif data.get("bonus_history", {}).get("issue"):
                    self.action_required(
                        "bonus_history_unavailable",
                        "Export history cannot establish the bonus counter. Check Recorder "
                        "and the selected export sensors. The app accepts manual export only "
                        "for a missing period before a validated statistics reference.",
                    )
                elif "fixed_pv_export_limit" in data.get("installation", {}).get("warnings", []):
                    self.action_required(
                        "fixed_pv_export_limit",
                        "The grid export limit is below the combined fixed-PV inverter ratings. "
                        "Sunny intervals can have no feasible plan when local consumption and storage cannot absorb production. "
                        "Check the ratings and configure PV on/off permission only if your automation can implement it. "
                        "Automatic proportional export limiting is not modeled. A grid limit does not grant PV control permission.",
                    )
                elif data.get("savings_status", {}).get("action_required"):
                    self.action_required(
                        "savings_unavailable",
                        "The savings comparison needs attention: "
                        + str(data["savings_status"].get("issue", "Unknown settlement issue"))
                        + ". Open Plan and savings in the app. Physical commands remain governed by their own plan validity.",
                    )
                else:
                    if self.issue:
                        persistent_notification.async_dismiss(
                            self.hass, f"{DOMAIN}.{self.entry.entry_id}"
                        )
                    self.issue = None
                    self.notification_message = None
                from .appliance_history import synchronize as synchronize_appliance_history

                self.refresh()
                await synchronize_appliance_history(self)
                if self.closed or self.schedule.resume_after != intent_at_request:
                    return
                for request_key, bridge, endpoint, label in (
                    (
                        "forecast_quality_request",
                        self.forecast_history_bridge,
                        "/v1/forecast-quality/history",
                        "Forecast quality",
                    ),
                    ("settlement_request", self.settlement_bridge, "/v1/settlement", "Settlement"),
                    (
                        "settlement_bonus_request",
                        self.settlement_bonus_bridge,
                        "/v1/settlement/bonus-history",
                        "Historical bonus",
                    ),
                ):
                    pending_history = data.get(request_key)
                    if not pending_history:
                        continue
                    # Publish received control decisions before optional Recorder/network work.
                    self.refresh()
                    try:
                        historical = await bridge.capture(pending_history)
                        if historical is not None:
                            await self.client.request("POST", endpoint, historical)
                    except AuthenticationError:
                        raise
                    except ReleaseMismatchError:
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        _LOGGER.warning(
                            "%s transfer failed. Retained commands remain independent",
                            label,
                            exc_info=True,
                        )
                    if self.closed or self.schedule.resume_after != intent_at_request:
                        return
            except ReleaseMismatchError:
                self.action_required(
                    "release_mismatch",
                    "Update the app and integration together to the same version and build. "
                    "Existing commands remain usable only within their normal validity.",
                )
            except AuthenticationError:
                if self.entry.data.get("supervisor"):
                    self.issue = "supervisor_reconnecting"
                    return
                if self.entry.data.get("fingerprint"):
                    self.entry.async_start_reauth(self.hass)
                self.action_required(
                    "authentication_failed",
                    "Open the app to check its Home Assistant connection and approve re-pairing when requested. "
                    "Existing commands remain usable only within their normal validity.",
                )
            except ServerFingerprintMismatch:
                self.action_required(
                    "identity_changed",
                    "The app certificate differs from the confirmed identity. "
                    "Restore the original app identity or explicitly pair again. "
                    "The saved certificate pin has not been replaced.",
                )
            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                if not self.issue:
                    _LOGGER.warning(
                        "EMS synchronization failed. Keeping only locally valid commands"
                    )
                self.issue = "synchronization_failed"
                # A temporary outage is not itself an action-required notification.
            finally:
                self.refresh()

    def action_required(self, reason, message):
        """Refresh changed recovery advice while deduplicating identical persistent messages."""
        if self.notification_message != message:
            _LOGGER.error("EMS requires attention: %s", reason)
            persistent_notification.async_create(
                self.hass,
                message,
                title="Open Energy Planner requires attention",
                notification_id=f"{DOMAIN}.{self.entry.entry_id}",
            )
            self.notification_message = message
        self.issue = reason

    def start(self):
        """Start polling and refresh startup metadata once entities are loaded. Boundaries keep their own timer."""

        async def started(_event):
            """Replace early placeholder metadata without waiting for the normal catalogue refresh period."""
            self.startup_cancel = None
            self.catalogue_sent = None
            await self.sync()

        if not self.hass.is_running:
            self.startup_cancel = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, started
            )
        self.poll_cancel = async_track_time_interval(
            self.hass, self.sync, timedelta(seconds=POLL_INTERVAL_SECONDS)
        )

    async def close(self):
        """Reject new exchanges, drain active synchronization and release listeners before HA unload."""
        self.closed = True
        self.catalogue_discovery.close()
        await self.market_prices.close()
        if self.appliance_history_task:
            self.appliance_history_task.cancel()
            try:
                await self.appliance_history_task
            except (asyncio.CancelledError, AuthenticationError, ReleaseMismatchError):
                pass
        if self.boundary_cancel:
            self.boundary_cancel()
        if self.poll_cancel:
            self.poll_cancel()
        if self.startup_cancel:
            self.startup_cancel()
        if self.ev_cancel:
            self.ev_cancel()
        if self.thermal_cancel:
            self.thermal_cancel()
        async with self.lock:
            self.solar_forecast_bridge.close()
            await self.save()
