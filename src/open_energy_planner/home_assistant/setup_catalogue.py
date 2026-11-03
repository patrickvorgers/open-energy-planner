"""Admit HA setup discovery and reconcile current domain ownership without platform installation."""

from dataclasses import replace
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.demand.calculation.model import EnergyTerm
from open_energy_planner.home_assistant.catalogue import (
    BATTERY_ENERGY_FIELDS,
    MAX_CATALOGUE_REQUEST_BYTES,
)
from open_energy_planner.installation.api import catalogue as installation_catalogue
from open_energy_planner.installation.storage import sources
from open_energy_planner.storage.writes import complete_write


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
                source_item
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
                await self.persist(
                    self.solar.store.save, candidate, self.solar.configure, contract_input=False
                )
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
