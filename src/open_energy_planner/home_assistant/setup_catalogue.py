"""Admit HA setup discovery and reconcile current domain ownership without platform installation."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.demand.calculation.model import EnergyTerm
from open_energy_planner.home_assistant.catalogue import (
    BATTERY_ENERGY_FIELDS,
    MAX_CATALOGUE_REQUEST_BYTES,
)
from open_energy_planner.home_assistant.names import source_names
from open_energy_planner.home_assistant.solar_panels import (
    PanelSettings,
    PanelState,
    follow_panels,
    panel_settings,
)
from open_energy_planner.installation.api import catalogue as installation_catalogue
from open_energy_planner.installation.storage import sources
from open_energy_planner.notifications.model import NoticeText, Notification, Severity
from open_energy_planner.storage.writes import complete_write

PANEL_CHANGE_NOTICE_MINUTES = 5
_LOGGER = logging.getLogger(__name__)


def panel_summary(source):
    """Describe the accepted physical planes without exposing provider credentials or location."""
    return ", ".join(
        f"{panel.capacity_w:g} Wp at {panel.azimuth:g}° with {panel.tilt:g}° tilt"
        for panel in source.geometry
    )


class SetupDiscovery:
    """Reconcile authenticated HA metadata with current installation and retained domain settings."""

    def __init__(
        self,
        catalogue,
        installation,
        demand,
        solar,
        lock,
        live,
        persist,
        required,
        contracts,
        appliances,
    ):
        """Borrow the current stores and serialize discovery under the same app configuration lock."""
        self.catalogue = catalogue
        self.installation = installation
        self.demand = demand
        self.solar = solar
        self.lock = lock
        self.live = live
        self.persist = persist
        self.required = required
        self.contracts = contracts
        self.appliances = appliances
        self.panel_changes = {}

    def panel_settings(self):
        """Present last-known ownership separately from whether fresh metadata is currently readable."""
        now = datetime.now(UTC)
        current = self.catalogue.current(now)
        sources = (
            {source.entity_id: source for source in self.solar.configuration.sources}
            if self.solar.configuration
            else {}
        )
        entities = set(sources)
        if current["data"]:
            entities.update(current["data"]["suggestions"]["solar_sources"])
        result = {}
        for entity in sorted(entities):
            source = sources.get(entity)
            settings = panel_settings(current, entity, source)
            if source and follow_panels(source, settings) != source:
                settings = PanelSettings(PanelState.UNAVAILABLE, source.ha_imports)
            result[entity] = settings.document()
        return result

    def panel_notifications(self):
        """Show current synchronization outages and short acknowledged ownership-change notices."""
        now = datetime.now(UTC)
        settings = self.panel_settings()
        self.panel_changes = {
            entity: (until, notice)
            for entity, (until, notice) in self.panel_changes.items()
            if entity in settings and now < until
        }
        names = source_names(self.catalogue.value)
        result = [notice for until, notice in self.panel_changes.values()]
        for entity, status in settings.items():
            if status["state"] == PanelState.UNAVAILABLE:
                result.append(
                    Notification(
                        "solar-panels:unavailable:" + entity,
                        "solar_panel_settings_unavailable",
                        NoticeText(
                            "Panel settings unavailable: " + names.get(entity, "Solar production"),
                            (
                                "Panel settings could not be refreshed. Last accepted settings are retained. "
                                if self.solar.configuration
                                and any(
                                    source.entity_id == entity
                                    for source in self.solar.configuration.sources
                                )
                                else "No panel settings have been accepted yet. Waiting for complete Home Assistant settings. "
                            )
                            + "Home Assistant remains in control. The connection is retried on the next configuration refresh.",
                            "View panel settings",
                            "/setup",
                        ),
                    )
                )
        return tuple(result)

    def record_panel_changes(self, before, after):
        """Announce accepted transitions only after the shared store acknowledges the new configuration."""
        now = datetime.now(UTC)
        names = source_names(self.catalogue.value)
        previous = {source.entity_id: source for source in before.sources}
        for source in after.sources:
            old = previous[source.entity_id]
            if (old.panels, old.ha_imports) == (source.panels, source.ha_imports):
                continue
            if source.ha_imports:
                message = "Home Assistant panel settings are now followed. "
                if not old.ha_imports:
                    message += "Previous manual settings: " + panel_summary(old) + ". "
                message += "Current settings: " + panel_summary(source) + "."
                code = "solar_panel_settings_following"
            else:
                message = (
                    "The Forecast.Solar link was removed in Home Assistant. "
                    "The last accepted panel settings are retained. You can now edit them manually."
                )
                code = "solar_panel_settings_manual"
            self.panel_changes[source.entity_id] = (
                now + timedelta(minutes=PANEL_CHANGE_NOTICE_MINUTES),
                Notification(
                    "solar-panels:changed:" + source.source_id,
                    code,
                    NoticeText(
                        "Panel settings changed: "
                        + names.get(source.entity_id, "Solar production"),
                        message,
                        "View panel settings",
                        "/setup",
                        Severity.INFO,
                    ),
                    auto_clear_minutes=PANEL_CHANGE_NOTICE_MINUTES,
                ),
            )
            _LOGGER.info(
                "Solar panel settings synchronized (%s). Forecast inputs were updated.", code
            )

    async def reconcile(self):
        """Archive source removals and balance updates without inventing device ratings.

        Apply the discovered HA timezone to demand configuration and bonus clocks.
        Persist affected settings. Changed bonus clocks receive a new recorded_at.
        Original contract revisions and plans remain immutable."""
        hints = self.catalogue.value["suggestions"]
        if hints["energy_status"] != "ready":
            return
        if self.contracts and self.contracts.current:
            current = self.contracts.current
            bonuses = tuple(
                replace(rule, timezone=self.catalogue.value["timezone"]) for rule in current.bonuses
            )
            if bonuses != current.bonuses:
                await self.persist(
                    self.contracts.save,
                    replace(current, recorded_at=datetime.now(UTC), bonuses=bonuses),
                )
        if self.solar.configuration:
            current = self.solar.configuration
            retained = tuple(
                follow_panels(
                    source_item,
                    panel_settings(
                        self.catalogue.current(datetime.now(UTC)),
                        source_item.entity_id,
                        source_item,
                    ),
                )
                for source_item in current.sources
                if source_item.entity_id in hints["solar_sources"]
            )
            candidate = replace(
                current,
                sources=retained,
                inverters=tuple(
                    inverter
                    for inverter in current.inverters
                    if any(
                        source_item.inverter_id == inverter.inverter_id for source_item in retained
                    )
                ),
            )
            if candidate != current:
                try:
                    await self.persist(
                        self.solar.store.save, candidate, self.solar.configure, contract_input=False
                    )
                finally:
                    # A drained write can become authoritative before cancellation propagates.
                    if self.solar.configuration == candidate:
                        self.record_panel_changes(current, candidate)
        if self.installation.current:
            current = self.installation.current
            retained = []
            for battery in current.batteries:
                hint = next(
                    (
                        battery_item
                        for battery_item in hints["batteries"]
                        if battery_item["energy_entity"] == battery.energy_entity
                        or (
                            battery.energy_entity is None
                            and (
                                battery_item["soc_entity"] == battery.soc_entity
                                or battery_item["power_entity"] == battery.power_entity
                            )
                        )
                    ),
                    None,
                )
                if hint:
                    fields = {"energy_entity": hint["energy_entity"]}
                    if "capacity_kwh" in hint and all(
                        hint.get(key) is not None for key in BATTERY_ENERGY_FIELDS
                    ):
                        fields.update({key: hint[key] for key in BATTERY_ENERGY_FIELDS})
                        fields["power_inverted"] = False
                    retained.append(replace(battery, **fields))
            grid_fields = {}
            if hints.get("grid_power_entities"):
                grid_fields = {
                    "grid_power_entity": hints["grid_power_entities"][0],
                    "additional_grid_power_entities": tuple(hints["grid_power_entities"][1:]),
                    "grid_power_inverted": False,
                    "grid_export_entity": hints["grid_export"],
                    "additional_grid_export_entities": tuple(
                        hints.get("settlement", {}).get("exports", [])[1:]
                    ),
                }
            candidate = replace(
                current,
                **grid_fields,
                batteries=tuple(retained),
                inverters=tuple(
                    inverter
                    for inverter in current.inverters
                    if any(
                        battery_settings.inverter_id == inverter.inverter_id
                        for battery_settings in retained
                    )
                ),
            )
            if candidate != current:
                await self.persist(
                    self.installation.save,
                    candidate,
                    lambda value: self.live.install_configuration(sources(value)),
                )
        if self.demand.configuration:
            candidate = replace(
                self.demand.configuration,
                terms=tuple(EnergyTerm(**energy_term) for energy_term in hints["terms"]),
                timezone=self.catalogue.value["timezone"],
            )
            if candidate != self.demand.configuration:
                await self.persist(
                    self.demand.store.save, candidate, self.demand.configure, contract_input=False
                )

    async def discovery(self, request):
        """Accept metadata and compact Recorder endpoints from the integration, never HA credentials or raw state history."""
        if self.live is None:
            raise web.HTTPConflict(text="Setup discovery requires Home Assistant mode")
        try:
            body = await request.clone(client_max_size=MAX_CATALOGUE_REQUEST_BYTES).json()
            async with self.lock:
                self.catalogue.receive(body, datetime.now(UTC))
                consumer_version = self.demand.consumers.version
                self.demand.consumers.sync(body)
                if self.demand.consumers.version != consumer_version:
                    self.demand.select_learned(False)
                    self.demand.candidate = None
                    self.demand.candidate_features = "consumers:" + self.demand.consumers.version
                cancellation = None
                if self.appliances is not None:
                    cancellation = await complete_write(self.appliances.sync_sources)
                issue = self.live.configuration_issue()
                if issue:
                    self.live.invalidate(issue)
                await self.reconcile()
                self.live.refresh()
                if cancellation:
                    raise cancellation
                result = {
                    "accepted": True,
                    "configuration_issue": self.live.configuration_issue(),
                    "devices": self.catalogue.devices(
                        installation_catalogue(
                            self.installation, self.solar, self.live.current_snapshot
                        )
                    )
                    if self.required or self.installation.current is not None
                    else None,
                }
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid setup discovery") from None
        return web.json_response(result)
