"""Own local command validity and independent HA data exchange."""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError, ServerFingerprintMismatch
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
from .client import (
    AuthenticationError,
    InstallationMismatchError,
    ReleaseMismatchError,
    SupersededInstallationExchangeError,
)
from .const import DOMAIN, RELEASE, projection_id
from .history import HistoryBridge
from .market_prices import MarketPriceBridge
from .observation_bridge import ObservationBridge
from .observation_push import ObservationPush
from .plan_status_watch import watch_plan_status
from .protocol import RevokedPlan, Schedule, identifier, timestamp, validate_device_configuration
from .settlement import SettlementBridge
from .snapshot_notifications import select_snapshot_notification
from .solar_forecasts import SolarForecastBridge
from .wire_limits import MAX_SNAPSHOT_RESPONSE_BYTES, validate_projection_size

CATALOGUE_RETRY_MINUTES = 5
CATALOGUE_CONFIRM_SECONDS = 60
SOLAR_FORECAST_REFRESH_MINUTES = 5
PLAN_WATCH_RETRY_SECONDS = 5
SERVICE_REQUEST_INTERVAL_SECONDS = 15
PLAN_CONNECTION_ISSUES = {
    "installation_identity_changed",
    "release_mismatch",
    "supervisor_reconnecting",
    "authentication_failed",
    "identity_changed",
    "synchronization_failed",
}


_LOGGER = logging.getLogger(__name__)


class PlanCoordinator:
    """Own HA enable intent, the command projection and independent data exchange timers."""

    def __init__(self, hass, entry, client):
        """Initialize entry-local state, storage, listeners and the synchronization lock."""
        self.hass, self.entry, self.client = hass, entry, client
        self.schedule = Schedule()
        self.store = Store(hass, 1, f"{DOMAIN}.{projection_id(entry)}")
        self.listeners = []
        self.projection = self.schedule.project(dt_util.utcnow())
        self.boundary_cancel = None
        self.plan_watch_task = None
        self.startup_cancel = None
        self.lock = asyncio.Lock()
        self.services_lock = asyncio.Lock()
        self.observations_lock = asyncio.Lock()
        self.service_cancel = None
        self.observation_task = None
        self.pending_enabled = None
        self.issue = None
        self.notification_message = None
        self.planning_status = None
        self.closed = False
        self.observation_bridge = None
        self.observation_generation = 0
        self.observation_push = ObservationPush(
            self._subscribe_observation_sources, self.sync_observations
        )
        self.observations = None
        self.service_diagnostics = {}
        self.devices = None
        self.consumer_forecasts = {}
        self.appliances = {}
        self.appliance_history_attempts = {}
        self.appliance_history_task = None
        self.appliance_raw_history_future = None
        self.appliance_observer_session = str(uuid4())
        self.appliance_history_cancel = None
        self.solar_feedback_task = None
        self.solar_feedback_attempts = {}
        self.solar_feedback_reads = {}
        self.ev = {}
        self.ev_intent_generation = 0
        self.ev_writes_pending = 0
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
        if self.projection.command and any(
            target.kind == "ev" for target in self.projection.command.devices
        ):
            from .ev import command as ev_command

            points = {point["id"]: point for point in self.ev.get("points", [])}
            targets = tuple(
                replace(target, power_w=None)
                if target.kind == "ev"
                and (
                    target.device_id not in points
                    or ev_command(self, points[target.device_id]) is None
                )
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
                    "grid_control_mode": command.grid_control_mode if command else None,
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
        await self.sync_plan()

    async def _publish_catalogue(self, now, *, force=False):
        """Publish one captured generation through the existing bounded transfer and acknowledge it.

        The caller owns the synchronization lock and failure recovery. Changes during
        preparation or HTTP transfer leave discovery dirty and locally unconfirmed.
        """
        from .catalogue_transfer import prepare_catalogue

        if self.closed or self.catalogue_bindings_rejected:
            return
        catalogue = await self.catalogue_discovery.capture(force=force, now=now)
        if self.closed or self.catalogue_bindings_rejected or catalogue is None:
            return
        generation = self.catalogue_discovery.captured_generation
        prepared = await self.hass.async_add_executor_job(prepare_catalogue, catalogue)
        if self.closed or self.catalogue_bindings_rejected:
            return
        if generation != self.catalogue_discovery.generation:
            self.catalogue_confirmed_at = None
            return
        command_revision = self.schedule.config_revision
        for page in prepared.pages:
            if self.closed or self.catalogue_bindings_rejected:
                return
            membership = await self.client.request("POST", "/v1/ha/catalogue", page)
        if membership.get("accepted") is not True:
            raise ValueError("Catalogue snapshot was not accepted")
        if self.closed:
            return
        # A changed watch affects freshness. Only replacement of the evaluated command
        # authority makes a genuine rejection obsolete.
        if (
            membership.get("configuration_issue")
            and self.schedule.config_revision != command_revision
            and self.schedule.config_revision != membership["config_revision"]
        ):
            membership = {**membership, "configuration_issue": None}
        if generation != self.catalogue_discovery.generation:
            self.catalogue_confirmed_at = None
            return membership
        self.catalogue_discovery.acknowledge(catalogue)
        self.catalogue_context = prepared
        self.catalogue_confirmed_at = now
        self.solar_forecast_sent = None
        self.solar_forecast_attempted_at = None
        return membership

    async def _confirm_catalogue(self, now):
        """Confirm healthy watched metadata even when independent command transport fails.

        The app also checks current source bindings before advancing freshness.
        Changed watches and rejected bindings never authorize this fallback.
        """
        if (
            self.closed
            or self.catalogue_bindings_rejected
            or self.catalogue_context is None
            or not self.catalogue_discovery.healthy
            or self.catalogue_confirmed_at is not None
            and now - self.catalogue_confirmed_at < timedelta(seconds=CATALOGUE_CONFIRM_SECONDS)
        ):
            return
        generation = self.catalogue_discovery.generation
        try:
            await self.client.request(
                "POST",
                "/v1/ha/catalogue",
                {"confirmed_revision": self.catalogue_context.revision},
            )
            if self.closed:
                return
            if generation != self.catalogue_discovery.generation:
                self.catalogue_confirmed_at = None
                return await self._publish_catalogue(now)
            else:
                self.catalogue_confirmed_at = now
        except (
            AuthenticationError,
            InstallationMismatchError,
            ReleaseMismatchError,
            ServerFingerprintMismatch,
        ):
            raise
        except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
            if not self.closed:
                self.catalogue_discovery.repair()
                self.catalogue_failed_at = None
            _LOGGER.debug("Setup catalogue confirmation failed", exc_info=True)

    async def write_ev(self, operation, body):
        """Withhold EV permission throughout uncertain intent and reconcile only from fresh state."""
        self.ev_writes_pending += 1
        self.ev_intent_generation += 1
        self.ev = {**self.ev, "revision": None}
        try:
            self.refresh()
            response = await self.client.request("POST", "/v1/ev/" + operation, body)
        finally:
            self.ev_writes_pending -= 1
            self.ev_intent_generation += 1
            self.ev = {**self.ev, "revision": None}
            self.refresh()
        await self.sync()
        return response

    async def sync(self, _now=None):
        """Reconcile once and run optional service work independently of the command lane."""

        async def sync_current():
            """Resolve source bindings before sending the first current capture."""
            reconciled = await self.sync_plan()
            observed = self.observation_task is None and await self.sync_observations()
            if reconciled and observed:
                await self.sync_plan()

        await asyncio.gather(sync_current(), self.sync_services())

    async def sync_plan(self, _now=None):
        """Accept only the app's current authorized command version and report reconciliation."""
        if self.closed:
            return False
        async with self.lock:
            if self.closed:
                return False
            try:
                if self.observation_bridge and not self._capture_required_sources():
                    self.revoke_observation_authority()
                generation = (self.schedule.resume_after, self.ev_intent_generation)
                if self.pending_enabled is not None:
                    await self.client.enabled(
                        self.pending_enabled, self.schedule.resume_after.isoformat()
                    )
                    if not self._same_intent(generation):
                        return False
                    self.pending_enabled = None
                    return True
                status = await self.client.plan_status()
                if not self._same_intent(generation):
                    return False
                enabled, revision, measurements_required, plan_id = await self._apply_plan_status(
                    status
                )
                if status.get("command_suspended") is True:
                    return True
                try:
                    if measurements_required and (
                        self.observation_bridge is None
                        or self.observation_bridge.revision != revision
                    ):
                        self.schedule.invalidate()
                        self.refresh()
                        reply = await self.client.measurement_config(revision)
                        if not self._same_intent(generation):
                            return False
                        configuration = reply["measurement_config"]
                        if configuration["revision"] != revision:
                            raise ValueError("Measurement configuration revision mismatch")
                        self._configure_measurements(configuration)
                    elif not measurements_required and self.observation_bridge is not None:
                        self._configure_measurements(None)
                    if plan_id is None:
                        return not enabled or self.schedule.enabled
                    if not self.schedule.enabled or (
                        measurements_required and not self._capture_required_sources()
                    ):
                        self.revoke_observation_authority()
                        return False
                    observation_generation = self.observation_generation
                    if self.schedule.plan and self.schedule.plan.plan_id == plan_id:
                        if status.get("acknowledged_plan") != plan_id:
                            await self.client.acknowledge(plan_id)
                        if (
                            not self._same_intent(generation)
                            or observation_generation != self.observation_generation
                        ):
                            return False
                        if measurements_required and not self._capture_required_sources():
                            self.revoke_observation_authority()
                            return False
                        self.schedule.suspended = False
                        return self.schedule.plan is not None
                    reply = await self.client.plan(plan_id)
                    if (
                        not self._same_intent(generation)
                        or observation_generation != self.observation_generation
                    ):
                        return False
                    plan = reply["plan"]
                    if plan.get("plan_id") != plan_id:
                        raise ValueError("Command plan identity mismatch")
                    self._validate_command_plan(
                        plan, reply.get("devices"), reply.get("ev_points", [])
                    )
                    candidate = self.schedule.validate_candidate(plan, dt_util.utcnow())
                    if (
                        self.schedule.resume_after
                        and candidate.generated_at <= self.schedule.resume_after
                    ):
                        return False
                    if status.get("acknowledged_plan") != plan_id:
                        await self.client.acknowledge(plan_id)
                    if not self._same_intent(generation):
                        return False
                    latest = await self.client.plan_status()
                    if (
                        not self._same_intent(generation)
                        or observation_generation != self.observation_generation
                    ):
                        return False
                    authorization = await self._apply_plan_status(latest)
                    if (
                        authorization != (True, revision, measurements_required, plan_id)
                        or latest.get("acknowledged_plan") != plan_id
                    ):
                        return False
                    if measurements_required and not self._capture_required_sources():
                        self.revoke_observation_authority()
                        return False
                    self.schedule.install_candidate(candidate, plan, dt_util.utcnow())
                    self.schedule.suspended = False
                except RevokedPlan:
                    return False
                except ClientResponseError as error:
                    if error.status != HTTPStatus.CONFLICT:
                        raise
                    self.schedule.invalidate()
                    return False
                return True
            except SupersededInstallationExchangeError:
                return False
            except (
                InstallationMismatchError,
                ReleaseMismatchError,
                AuthenticationError,
                ServerFingerprintMismatch,
            ) as error:
                self._connection_failure(error)
            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                if not self.issue:
                    _LOGGER.warning(
                        "EMS synchronization failed. Keeping only locally valid commands"
                    )
                self.issue = "synchronization_failed"
                # A temporary outage is not itself an action-required notification.
            finally:
                self.refresh()
            return False

    async def _apply_plan_status(self, status):
        """Apply durable enable intent and fail closed independently of publication acceptance."""
        if status.get("release") != RELEASE:
            raise ReleaseMismatchError("Plan status release mismatch")
        enabled = status.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("Invalid plan enable state")
        revision = identifier(status.get("config_revision"))
        measurements_required = status.get("measurements_required")
        if type(measurements_required) is not bool:
            raise ValueError("Invalid measurement requirement")
        plan_id = status.get("plan_id")
        if plan_id is not None:
            identifier(plan_id)
        suspended = status.get("command_suspended", False)
        if type(suspended) is not bool or suspended and (not enabled or plan_id is not None):
            raise ValueError("Invalid temporary command suspension")

        if revision != self.schedule.config_revision:
            self.schedule.invalidate()
            self.schedule.config_revision = revision
            self.refresh()
        self.planning_status = status.get("planning")
        self._clear_plan_connection_issue()
        if not enabled and self.schedule.enabled:
            self.schedule.pause(dt_util.utcnow())
            self.schedule.invalidate()
            self.refresh()
            await self.save()
        elif enabled and not self.schedule.enabled:
            self.pending_enabled = False
        if suspended:
            self.schedule.suspended = True
            self.refresh()
        elif plan_id is None:
            self.schedule.invalidate()
            self.refresh()
        return enabled, revision, measurements_required, plan_id

    def _same_intent(self, generation):
        """Reject observations from a request that outlived a newer local action."""
        return not self.closed and generation == (
            self.schedule.resume_after,
            self.ev_intent_generation,
        )

    def _connection_failure(self, error):
        """Apply existing identity, authentication and paired-build recovery behavior."""
        if self.closed:
            return
        if isinstance(error, InstallationMismatchError):
            self.revoke_observation_authority()
            self.pending_enabled = None
            self.action_required(
                "installation_identity_changed",
                "This Home Assistant connection belongs to a different OEP installation. "
                "Existing commands have been withdrawn. Verify the current app connection to resume.",
            )
        elif isinstance(error, ReleaseMismatchError):
            self.action_required(
                "release_mismatch",
                "Update the app and integration together to the same version and build. "
                "Existing commands remain usable only within their normal validity.",
            )
        elif isinstance(error, AuthenticationError):
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
        elif isinstance(error, ServerFingerprintMismatch):
            self.action_required(
                "identity_changed",
                "The app certificate differs from the confirmed identity. "
                "Restore the original app identity or explicitly pair again. "
                "The saved certificate pin has not been replaced.",
            )

    def _clear_plan_connection_issue(self):
        """Only a validated status response clears an earlier command transport issue."""
        if self.issue in PLAN_CONNECTION_ISSUES:
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}.{self.entry.entry_id}")
            self.issue = None
            self.notification_message = None

    def _capture_required_sources(self):
        """Check required local inputs without depending on the app connection."""
        ready, _report = self.observation_bridge.capture(dt_util.utcnow())
        return ready

    def _validate_command_plan(self, plan, devices, ev_points):
        """Reject commands that exceed independently reported installation capabilities."""
        if devices is not None:
            expected = {
                device_item["id"]: device_item["kind"]
                for device_item in devices
                if device_item.get("configured", True)
                and (device_item["kind"] == "battery" or device_item.get("can_curtail", True))
            }
            ev_points = {point["id"]: point for point in ev_points}
            for work in plan.get("ev_context", {}).get("work", []):
                point = ev_points.get(work["point_id"])
                if not point or point["control"] == "read_only":
                    raise ValueError("Plan uses an unconfigured charge point")
                expected[work["point_id"]] = "ev"
            # The immutable plan owns thermal membership. Protocol parsing
            # validates its complete context. Optional metadata faults or
            # changed bindings suppress only each heater's local guard.
            for heater in plan.get("thermal_context", {}).get("devices", []):
                expected[heater["id"]] = "thermal"
            followers = {
                device_item["id"]
                for device_item in devices
                if device_item.get("can_follow_grid") is True
            }
            if plan.get("device_commands") is not True or any(
                {key: value.get("kind") for key, value in step.get("devices", {}).items()}
                != expected
                for step in plan.get("steps", [])
            ):
                raise ValueError("Plan does not match configured device capabilities")
            if any(
                value.get("mode") == "follow_grid_target" and key not in followers
                for step in plan.get("steps", [])
                for key, value in step.get("devices", {}).items()
            ):
                raise ValueError("Plan uses an unconfigured grid-following capability")

    def _configure_measurements(self, configuration):
        """Install the current revision of app-owned HA source bindings."""
        bridge = (
            ObservationBridge(self.hass.states, configuration)
            if configuration is not None
            else None
        )
        self.revoke_observation_authority()
        self.observation_bridge = bridge
        self.observations = None
        self.observation_push.configure(bridge)

    def _subscribe_observation_sources(self, entity_ids, notify):
        """Watch only the selected HA state events and withdraw known unsafe commands immediately."""

        @callback
        def state_changed(event):
            """Apply local source validity before queueing the independent network report."""
            if self.closed or self.observation_bridge is None:
                return
            if not self.observation_bridge.required_entity_ready(
                event.data["entity_id"], dt_util.utcnow()
            ):
                self.revoke_observation_authority()
            notify()

        return async_track_state_change_event(self.hass, entity_ids, state_changed)

    @callback
    def revoke_observation_authority(self):
        """Withdraw commands and prevent in-flight validation from bypassing a local source fault."""
        self.observation_generation += 1
        self.schedule.invalidate()
        self.refresh()

    async def sync_observations(self, _now=None):
        """Send one normalized current capture without waiting for plans or history."""
        if self.closed or self.observations_lock.locked() or self.observation_bridge is None:
            return False
        async with self.observations_lock:
            bridge = self.observation_bridge
            ready, report = bridge.capture(
                dt_util.utcnow(),
                location={
                    "latitude": self.hass.config.latitude,
                    "longitude": self.hass.config.longitude,
                    "timezone": self.hass.config.time_zone,
                },
            )
            if not ready:
                self.revoke_observation_authority()
            try:
                receipt = await self.client.observations(report)
                if self.closed or bridge is not self.observation_bridge:
                    return False
                if (
                    receipt.get("accepted") is not True
                    or receipt.get("config_revision") != bridge.revision
                ):
                    self.revoke_observation_authority()
                    return False
                self.observations = receipt.get("observations")
                self._update_snapshot_notification(self.service_diagnostics)
            except SupersededInstallationExchangeError:
                return False
            except InstallationMismatchError as error:
                self._connection_failure(error)
                return False
            except ClientResponseError as error:
                if (
                    not self.closed
                    and error.status == HTTPStatus.CONFLICT
                    and bridge is self.observation_bridge
                ):
                    self.revoke_observation_authority()
                return False
            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                _LOGGER.debug("Current observations transfer did not complete", exc_info=True)
                return False
            return True

    async def sync_services(self, _now=None):
        """Service setup, optional virtual projections and Recorder away from plan reconciliation."""
        if self.closed or self.services_lock.locked():
            return
        async with self.services_lock:
            try:
                intent_at_request = self.schedule.resume_after
                ev_intent_at_request = self.ev_intent_generation
                data = await self.client.service_requests()
                if self.closed:
                    return
                if data.get("release") != RELEASE:
                    raise ReleaseMismatchError("HA data requests release mismatch")
                if data.get("source") == "home_assistant":
                    from .source_choices import refresh_choices

                    await refresh_choices(
                        self, data.get("installation", {}).get("source_choices_requested", [])
                    )
                    if self.closed:
                        return
                self.market_prices.start(data.get("market_price_request"), self.client)
                now = dt_util.utcnow()
                if data.get("source") == "home_assistant":
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
                    try:
                        membership = await self._publish_catalogue(
                            now, force=bool(forced_catalogue)
                        )
                        if membership is None:
                            membership = await self._confirm_catalogue(now)
                        if self.closed:
                            return
                        if membership is not None:
                            if membership.get("devices") is not None:
                                data["installation"]["devices"] = membership["devices"]
                            if membership.get("configuration_issue"):
                                self.revoke_observation_authority()
                                self.planning_status = {
                                    "status": "not_ready",
                                    "reason": membership["configuration_issue"],
                                }
                        self.catalogue_failed_at = None
                    except (
                        AuthenticationError,
                        InstallationMismatchError,
                        ReleaseMismatchError,
                        ServerFingerprintMismatch,
                    ):
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        self.catalogue_discovery.repair()
                        self.catalogue_failed_at = now
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
                    except (InstallationMismatchError, ReleaseMismatchError):
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        _LOGGER.debug("Solar forecast refresh failed", exc_info=True)
                if self.ev_intent_generation != ev_intent_at_request:
                    return
                if "ev" in data:
                    from .ev import capture as capture_ev
                    from .ev import receive as receive_ev
                    from .ev import watch_connections

                    self.ev = receive_ev(data["ev"])
                    watch_connections(self)
                    if self.ev["vehicles"] or self.ev["points"]:
                        ev_reply = await self.client.request(
                            "POST",
                            "/v1/ev/observations",
                            capture_ev(self, self.ev, dt_util.utcnow()),
                        )
                        if self.closed or self.ev_intent_generation != ev_intent_at_request:
                            return
                        self.ev = receive_ev(ev_reply)
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
                        except (
                            AuthenticationError,
                            InstallationMismatchError,
                            ReleaseMismatchError,
                        ):
                            raise
                        except (ClientError, TimeoutError):
                            # A transport outage retains only locally fresh, guarded permissions.
                            _LOGGER.debug("Thermal observations transfer failed", exc_info=True)
                        else:
                            if (
                                self.closed
                                or self.schedule.resume_after != intent_at_request
                                or self.ev_intent_generation != ev_intent_at_request
                            ):
                                return
                            self.thermal = receive_thermal(thermal_reply)
                except (AuthenticationError, InstallationMismatchError, ReleaseMismatchError):
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
                self.service_diagnostics = {
                    key: data[key]
                    for key in (
                        "installation",
                        "bonus_history",
                        "demand",
                        "solar",
                        "savings_status",
                    )
                    if key in data
                }
                self._update_snapshot_notification(self.service_diagnostics)
                self.refresh()
                from .solar_feedback import synchronize as synchronize_solar_feedback

                await synchronize_solar_feedback(self, data)
                if (
                    self.closed
                    or self.schedule.resume_after != intent_at_request
                    or self.ev_intent_generation != ev_intent_at_request
                ):
                    return
                for request_key, bridge, endpoint, label in (
                    (
                        "forecast_quality_request",
                        self.forecast_history_bridge,
                        "/v1/forecast-quality/history",
                        "Forecast quality",
                    ),
                    ("settlement_request", self.settlement_bridge, "/v1/settlement", "Settlement"),
                ):
                    pending_history = data.get(request_key)
                    if not pending_history:
                        continue
                    # Publish received control decisions before optional Recorder/network work.
                    self.refresh()
                    try:
                        historical = await bridge.capture(pending_history)
                        if self.closed:
                            return
                        if historical is not None:
                            await self.client.request("POST", endpoint, historical)
                    except AuthenticationError:
                        raise
                    except (InstallationMismatchError, ReleaseMismatchError):
                        raise
                    except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                        _LOGGER.warning(
                            "%s transfer failed. Retained commands remain independent",
                            label,
                            exc_info=True,
                        )
                    if (
                        self.closed
                        or self.schedule.resume_after != intent_at_request
                        or self.ev_intent_generation != ev_intent_at_request
                    ):
                        return
                for domain, bridge, endpoint in (
                    (
                        "bonus_history",
                        self.history_bridge,
                        "/v1/history",
                    ),
                    (
                        "demand",
                        self.demand_history_bridge,
                        "/v1/demand/history",
                    ),
                    (
                        "solar",
                        self.solar_history_bridge,
                        "/v1/solar/history",
                    ),
                ):
                    if self.closed:
                        return
                    pending = data.get(domain, {}).get("request")
                    if pending:
                        try:
                            historical = await bridge.capture(pending)
                            if self.closed:
                                return
                            if historical is not None:
                                receipt = await self.client.request("POST", endpoint, historical)
                                diagnostics = receipt.get("diagnostics")
                                if domain == "bonus_history" and "issue" in receipt:
                                    diagnostics = {"issue": receipt["issue"]}
                                if not self.closed and isinstance(diagnostics, dict):
                                    self.service_diagnostics[domain] = diagnostics
                                    self._update_snapshot_notification(self.service_diagnostics)
                        except (
                            InstallationMismatchError,
                            ReleaseMismatchError,
                            AuthenticationError,
                        ):
                            raise
                        except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                            _LOGGER.debug(
                                "Optional history transfer did not complete", exc_info=True
                            )
            except (
                InstallationMismatchError,
                ReleaseMismatchError,
                AuthenticationError,
                ServerFingerprintMismatch,
            ) as error:
                self._connection_failure(error)
            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                _LOGGER.debug("Non-command synchronization did not complete", exc_info=True)
            finally:
                self.refresh()

    def _update_snapshot_notification(self, data):
        """Apply the selected recovery advice or clear it after recovery."""
        if self.issue in PLAN_CONNECTION_ISSUES:
            return
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
        notification = select_snapshot_notification(
            data, self.planning_status, issues, required_sources
        )
        if notification is not None:
            self.action_required(*notification)
            return
        if self.issue:
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}.{self.entry.entry_id}")
        self.issue = None
        self.notification_message = None

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

    async def _wait_for_plan_status(self, cursor):
        """Follow the currently configured authenticated connection through reconnects."""
        return await self.client.watch_plan_status(cursor)

    def start(self):
        """Start the plan watch, source reports and independent service timers."""

        async def started(_event):
            """Replace early placeholder metadata without waiting for the normal catalogue refresh period."""
            self.startup_cancel = None
            self.catalogue_sent = None
            await self.sync_services()

        if not self.hass.is_running:
            self.startup_cancel = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, started
            )
        from .appliance_history import RETRY_SECONDS, synchronize

        async def refresh_history(now):
            """Refresh learning independently from forecast and physical command exchanges."""
            await synchronize(self, now)

        self.appliance_history_cancel = async_track_time_interval(
            self.hass, refresh_history, timedelta(seconds=RETRY_SECONDS)
        )
        self.hass.async_create_background_task(synchronize(self), "OEP appliance history startup")
        self.plan_watch_task = self.hass.async_create_background_task(
            watch_plan_status(
                self._wait_for_plan_status,
                self.sync_plan,
                has_pending_intent=lambda: self.pending_enabled is not None,
                retry_seconds=PLAN_WATCH_RETRY_SECONDS,
            ),
            "OEP plan status watch",
        )
        self.service_cancel = async_track_time_interval(
            self.hass, self.sync_services, timedelta(seconds=SERVICE_REQUEST_INTERVAL_SECONDS)
        )
        self.observation_task = self.hass.async_create_background_task(
            self.observation_push.run(), "OEP current observation reports"
        )

    async def close(self):
        """Reject new exchanges, drain active synchronization and release listeners before HA unload."""
        self.closed = True
        self.observation_push.close()
        exchange_tasks = tuple(
            task for task in (self.plan_watch_task, self.observation_task) if task is not None
        )
        for task in exchange_tasks:
            task.cancel()
        await asyncio.gather(*exchange_tasks, return_exceptions=True)
        self.catalogue_discovery.close()
        await self.market_prices.close()
        if self.solar_feedback_task:
            self.solar_feedback_task.cancel()
            try:
                await self.solar_feedback_task
            except (
                asyncio.CancelledError,
                AuthenticationError,
                ClientError,
                InstallationMismatchError,
                ReleaseMismatchError,
                TimeoutError,
            ):
                pass
        pending_feedback_reads = tuple(self.solar_feedback_reads.values())
        for task in pending_feedback_reads:
            task.cancel()
        await asyncio.gather(*pending_feedback_reads, return_exceptions=True)
        if self.appliance_history_cancel:
            self.appliance_history_cancel()
        if self.appliance_history_task:
            self.appliance_history_task.cancel()
            try:
                await self.appliance_history_task
            except (
                asyncio.CancelledError,
                AuthenticationError,
                ClientError,
                InstallationMismatchError,
                ReleaseMismatchError,
                TimeoutError,
            ):
                pass
        if self.boundary_cancel:
            self.boundary_cancel()
        if self.service_cancel:
            self.service_cancel()
        if self.startup_cancel:
            self.startup_cancel()
        if self.ev_cancel:
            self.ev_cancel()
        if self.thermal_cancel:
            self.thermal_cancel()
        async with self.services_lock, self.observations_lock, self.lock:
            self.solar_forecast_bridge.close()
            await self.save()
