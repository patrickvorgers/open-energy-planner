"""App-owned setup interface and bounded HA discovery metadata. No HA or device drivers."""

from dataclasses import asdict
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.demand.wire import parse_configuration as parse_demand
from open_energy_planner.home_assistant.catalogue import (
    COUNTRY_BIDDING_AREA_HINTS,
    COUNTRY_BIDDING_AREA_OPTIONS,
    MAX_CATALOGUE_REQUEST_BYTES,
    HACatalogue,
)
from open_energy_planner.home_assistant.setup_catalogue import SetupDiscovery
from open_energy_planner.installation.api import catalogue as installation_catalogue
from open_energy_planner.installation.calculation.annual_estimate import annual_estimate
from open_energy_planner.installation.model import InstallationProblem, PVUnit
from open_energy_planner.installation.storage import document, parse
from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.solar.wire import parse_configuration as parse_solar


def register(
    app,
    installation,
    demand,
    solar,
    lock,
    live,
    persist,
    *,
    required=False,
    contracts=None,
    catalogue=None,
    appliances=None,
):
    """Register public setup pages/metadata and authenticated configuration routes.

    Ready Energy discovery reconciles source membership, demand balance/timezone
    and, when contracts has a current revision, HA-owned bonus timezones. Changed
    bonus clocks persist a new revision and recorded_at. Original plans stay intact.
    """
    catalogue = catalogue or HACatalogue()
    if live:
        if required:
            catalogue.request_refresh()
        live.configuration_issue = lambda: catalogue.configuration_issue(
            installation.current, solar.configuration, demand.configuration, required=required
        )

    discovery_handler = SetupDiscovery(
        catalogue, installation, demand, solar, lock, live, persist, required, contracts, appliances
    )
    app[NOTIFICATIONS].register("solar-panels", discovery_handler.panel_notifications)

    async def settings(request):
        """Return app definitions and mandatory Energy discovery, requesting a refresh only when needed or explicit."""
        async with lock:
            if (
                catalogue.current(datetime.now(UTC))["stale"] or request.query.get("refresh") == "1"
            ) and catalogue.requested is None:
                catalogue.request_refresh()
            return web.json_response(
                {
                    "mode": "home_assistant" if live else "synthetic",
                    "panel_settings": discovery_handler.panel_settings(),
                    "installation": document(installation.current),
                    "solar": {**asdict(solar.configuration), "version": solar.configuration.version}
                    if solar.configuration
                    else None,
                    "demand": {
                        **asdict(demand.configuration),
                        "version": demand.configuration.version,
                    }
                    if demand.configuration
                    else None,
                    "catalogue": catalogue.current(datetime.now(UTC)),
                    "market_area_options": COUNTRY_BIDDING_AREA_OPTIONS.get(
                        (catalogue.value or {}).get("country"), ()
                    ),
                    "market_area_hint": COUNTRY_BIDDING_AREA_HINTS.get(
                        catalogue.value.get("country")
                    )
                    if catalogue.value
                    else None,
                    "devices": catalogue.devices(
                        installation_catalogue(
                            installation, solar, live.current_snapshot if live else None
                        )
                    ),
                    "annual_estimate": annual_estimate(
                        catalogue.value["suggestions"]["terms"], catalogue.value["history"]
                    )
                    if catalogue.value
                    else None,
                }
            )

    async def preview(request):
        """Check the complete prospective configuration before the wizard saves independently versioned definitions."""
        if live is None:
            raise web.HTTPConflict(text="Setup requires Home Assistant mode")
        current = catalogue.current(datetime.now(UTC))
        if (
            current["stale"]
            or not current["data"]
            or current["data"]["suggestions"]["energy_status"] != "ready"
        ):
            raise web.HTTPConflict(
                text="Connect Home Assistant and complete its Energy dashboard before setup"
            )
        try:
            body = await request.json()
            if set(body) != {"installation", "solar", "demand"}:
                raise ValueError("Complete setup required")
            if "expected_version" in body["solar"] and body["solar"]["expected_version"] != (
                solar.configuration.version if solar.configuration else None
            ):
                raise web.HTTPConflict(text="Solar configuration changed. Reload before saving")
            physical, pv = parse(body["installation"]), parse_solar(body["solar"])
            catalogue.validate_panel_settings(pv, solar.configuration, datetime.now(UTC))
            catalogue.validate_membership("installation", physical)
            load = parse_demand(body["demand"])
            catalogue.validate_membership("demand", load)
            hints = current["data"]["suggestions"]
            if {source.entity_id for source in pv.sources} != set(hints["solar_sources"]):
                raise ValueError("Solar sources must match the Energy dashboard")
            unmatched = list(physical.batteries)
            for hint in hints["batteries"]:
                match = next(
                    (
                        battery
                        for battery in unmatched
                        if battery.energy_entity == hint["energy_entity"]
                        or (
                            battery.energy_entity is None
                            and (
                                (hint["soc_entity"] and battery.soc_entity == hint["soc_entity"])
                                or (
                                    hint["power_entity"]
                                    and battery.power_entity == hint["power_entity"]
                                )
                            )
                        )
                    ),
                    None,
                )
                if match is None:
                    raise ValueError("Battery sensors must match the Energy dashboard")
                unmatched.remove(match)
            if unmatched:
                raise ValueError("Remove or restore saved batteries absent from Energy")
            if body["demand"]["terms"] != current["data"]["suggestions"]["terms"]:
                raise ValueError("Use the complete Energy dashboard household balance")
            entities = {
                entity_item["entity_id"]: entity_item for entity_item in current["data"]["entities"]
            }

            def sensor(entity, kind):
                """Require selected sensors to exist in the fresh typed HA catalogue."""
                item = entities.get(entity, {})
                units = {"power": ("W", "kW"), "energy": ("Wh", "kWh", "MWh"), "battery": ("%",)}
                if (
                    item.get("unit") not in units[kind]
                    or item.get("device_class") != kind
                    or (
                        kind == "energy"
                        and item.get("state_class") not in ("total", "total_increasing")
                    )
                ):
                    raise ValueError("Choose an available sensor of the required type")

            sensor(physical.grid_power_entity, "power")
            for entity in physical.additional_grid_power_entities:
                sensor(entity, "power")
            for term in load.terms:
                sensor(term.entity_id, "energy")
            if physical.grid_export_entity:
                sensor(physical.grid_export_entity, "energy")
            for entity in physical.additional_grid_export_entities:
                sensor(entity, "energy")
            for source in pv.sources:
                sensor(source.entity_id, "energy")
            for battery in physical.batteries:
                sensor(battery.soc_entity, "battery")
                sensor(battery.power_entity, "power")
            converters = {inverter.inverter_id: inverter for inverter in pv.inverters}
            for converter in physical.inverters:
                if (
                    converter.inverter_id in converters
                    and converters[converter.inverter_id] != converter
                ):
                    raise ValueError("Shared inverter definitions disagree")
                converters[converter.inverter_id] = converter
            # Reuse linked-device domain invariants without running a solver or inventing measured SoC.
            ids = {source_item.source_id for source_item in pv.sources}
            if ids & {battery_item.device_id for battery_item in physical.batteries}:
                raise ValueError("Device identities collide")
            for battery in physical.batteries:
                if (
                    any(
                        source_item.inverter_id == battery.inverter_id for source_item in pv.sources
                    )
                    and not converters[battery.inverter_id].hybrid
                ):
                    raise ValueError("PV and storage sharing requires a hybrid converter")
            InstallationProblem(
                (
                    Interval(
                        datetime.now(UTC),
                        1,
                        0,
                        0,
                        0,
                        0,
                        pv_sources=tuple((source_item.source_id, 0) for source_item in pv.sources),
                    ),
                ),
                tuple(converters.values()),
                (),
                tuple(
                    PVUnit(source_item.source_id, source_item.inverter_id, source_item.can_curtail)
                    for source_item in pv.sources
                ),
                physical.import_limit_w,
                physical.export_limit_w,
            )
            return web.json_response(
                {
                    "batteries": len(physical.batteries),
                    "solar_sources": len(pv.sources),
                    "annual_kwh": load.annual_kwh,
                }
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(
                text="Invalid setup: check units, ratings and shared inverter links"
            ) from None

    async def source_refresh(request):
        """Request or observe a source-only refresh without loading or changing saved configuration."""
        if live is None:
            raise web.HTTPConflict(text="Source refresh requires Home Assistant mode")
        async with lock:
            if request.method == "POST":
                catalogue.request_refresh()
            return web.json_response(catalogue.refresh_status())

    async def source_choices(request):
        """Request finite editor scopes through admin authorization, retaining active membership."""
        try:
            if request.method == "POST" and await request.json() != {}:
                raise ValueError("Invalid source choice request")
            async with lock:
                scope = request.match_info["scope"]
                now = datetime.now(UTC)
                if request.method == "POST":
                    catalogue.choices.request(scope, now)
                return web.json_response(catalogue.choices.status(scope, catalogue.value, now))
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(
                text="Source choices could not be refreshed. Retry from the appliance settings."
            ) from None

    async def receive_choices(request):
        """Validate the requested bounded peer result without running source reconciliation."""
        try:
            body = await request.clone(client_max_size=MAX_CATALOGUE_REQUEST_BYTES).json()
            async with lock:
                accepted = catalogue.choices.receive(body, datetime.now(UTC))
                return web.json_response({"accepted": accepted})
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid source choice response") from None

    app.router.add_post("/v1/ha/source-choices", receive_choices)
    app.router.add_get("/v1/ha/source-choices/{scope}", source_choices)
    app.router.add_post("/v1/ha/source-choices/{scope}", source_choices)
    app.router.add_post("/v1/ha/catalogue", discovery_handler.discovery)
    app.router.add_get("/v1/ha/catalogue/refresh", source_refresh)
    app.router.add_post("/v1/ha/catalogue/refresh", source_refresh)
    app.router.add_get("/v1/setup", settings)
    app.router.add_post("/v1/setup/preview", preview)
    return catalogue
