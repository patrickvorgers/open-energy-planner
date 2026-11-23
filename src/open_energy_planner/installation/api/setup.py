"""App-owned setup interface and bounded HA discovery metadata. No HA or device drivers."""

import logging
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
from open_energy_planner.installation.api import warnings
from open_energy_planner.installation.calculation.annual_estimate import annual_estimate
from open_energy_planner.installation.model import InstallationProblem, PVUnit
from open_energy_planner.installation.storage import document, parse, sources
from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.runtime.diagnostics.logging import request_error_message
from open_energy_planner.solar.wire import parse_configuration as parse_solar
from open_energy_planner.solar.wire import validate_configuration_dates
from open_energy_planner.storage.writes import complete_write

_LOGGER = logging.getLogger(__name__)


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
    source_entities,
):
    """Register public setup pages/metadata and authenticated configuration routes.

    Ready Energy discovery reconciles source membership, demand balance/timezone
    and, when contracts has a current revision, HA-owned bonus timezones. Changed
    bonus clocks persist a new revision and recorded_at. Original plans stay intact.
    source_entities supplies current persisted bindings for serialized metadata
    confirmation, using the same composition projection as command snapshots.
    """
    catalogue = catalogue or HACatalogue()
    if live:
        if required:
            catalogue.request_refresh()
        live.configuration_issue = lambda: catalogue.configuration_issue(
            installation.current, solar.configuration, demand.configuration, required=required
        )

    discovery_handler = SetupDiscovery(
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
        source_entities,
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
                    **setup_definitions(),
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

    def setup_definitions():
        """Project canonical settings without reading unrelated operational state."""
        return {
            "installation": document(installation.current),
            "solar": {**asdict(solar.store.current), "version": solar.store.current.version}
            if solar.store.current
            else None,
            "demand": {**asdict(demand.store.current), "version": demand.store.current.version}
            if demand.store.current
            else None,
        }

    def current_configurations():
        """Read the three settings owners under the application lock."""
        return {
            "installation": installation.current,
            "solar": solar.store.current,
            "demand": demand.store.current,
        }

    def check_versions(body, *, required):
        """Reject a stale form before changing any of its configuration owners."""
        for name, configuration in current_configurations().items():
            expected = configuration.version if configuration is not None else None
            if (required or "expected_version" in body[name]) and (
                "expected_version" not in body[name] or body[name]["expected_version"] != expected
            ):
                raise web.HTTPConflict(text="Installation settings changed. Reload before saving")

    def validated_setup(body, *, save=False):
        """Validate the complete proposal against one current source catalogue."""
        now = datetime.now(UTC)
        current = catalogue.current(now)
        if (
            current["stale"]
            or not current["data"]
            or current["data"]["suggestions"]["energy_status"] != "ready"
        ):
            raise web.HTTPConflict(
                text="Connect Home Assistant and complete its Energy dashboard before setup"
            )
        if set(body) != {"installation", "solar", "demand"}:
            raise ValueError("Complete setup required")
        check_versions(body, required=save)
        physical, pv = parse(body["installation"]), parse_solar(body["solar"])
        validate_configuration_dates(pv, now)
        catalogue.validate_panel_settings(pv, solar.configuration, now)
        catalogue.validate_membership("installation", physical)
        catalogue.validate_membership("solar", pv)
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
                any(source_item.inverter_id == battery.inverter_id for source_item in pv.sources)
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

        return {"installation": physical, "solar": pv, "demand": load}

    def save_setup(changes):
        """Commit configuration pointers together before applying any runtime changes."""
        stores = {"installation": installation, "solar": solar.store, "demand": demand.store}
        documents = [stores[name].documents for name in changes if name != "solar"]
        transaction = (
            solar.store.transaction(*documents)
            if "solar" in changes
            else documents[0].transaction(*documents[1:])
        )
        calibration = None
        with transaction:
            for name, candidate in changes.items():
                progress = stores[name].write_configuration(candidate)
                if name == "installation":
                    calibration = progress
        if "installation" in changes:
            installation.install_configuration(changes["installation"], calibration)
        for name in ("demand", "solar"):
            if name in changes:
                # An incomplete runtime update must not admit a previous forecast.
                stores[name].current, stores[name].issue = (
                    changes[name],
                    f"{name}_configuration_unreadable",
                )

    def restore_forecast_settings(changes):
        """Restore changed forecast owners after durable storage and old-plan revocation."""
        if "demand" in changes:
            demand.configure(changes["demand"])
            demand.store.issue = None
        if "solar" in changes:
            with solar.configuration_lock:
                solar.configure(changes["solar"])
                solar.store.issue = None

    async def preview(request):
        """Check a prospective setup without writing definitions or running a solver."""
        if live is None:
            raise web.HTTPConflict(text="Setup requires Home Assistant mode")
        try:
            body = await request.json()
            async with lock:
                proposal = validated_setup(body)
            return web.json_response(
                {
                    "batteries": len(proposal["installation"].batteries),
                    "solar_sources": len(proposal["solar"].sources),
                    "annual_kwh": proposal["demand"].annual_kwh,
                }
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(
                text="Invalid setup: check units, ratings and shared inverter links"
            ) from None

    async def configure(request):
        """Save only changed definitions and acknowledge without a planning or settlement read."""
        if live is None:
            raise web.HTTPConflict(text="Setup requires Home Assistant mode")
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid setup settings") from None
        try:
            async with lock:
                try:
                    proposal = validated_setup(body, save=True)
                except (ValueError, TypeError, KeyError, OverflowError):
                    raise web.HTTPBadRequest(
                        text="Invalid setup: check units, ratings and shared inverter links"
                    ) from None
                previous = current_configurations()
                changes = {
                    name: candidate
                    for name, candidate in proposal.items()
                    if previous[name] is None or candidate.version != previous[name].version
                }
                if changes:
                    measurement = (
                        sources(proposal["installation"]) if "installation" in changes else None
                    )
                    activated = False

                    def activate(_changes):
                        """Revoke obsolete commands before acknowledging the saved settings."""
                        nonlocal activated
                        if measurement is not None:
                            live.install_configuration(measurement)
                        else:
                            live.invalidate(
                                "demand_forecast_pending"
                                if "demand" in changes
                                else "solar_forecast_unavailable"
                            )
                        activated = True

                    try:
                        await persist(
                            save_setup,
                            changes,
                            activate,
                            contract_input=measurement is not None,
                            force_replan=False,
                            defer_refresh=True,
                        )
                    finally:
                        if activated and ("solar" in changes or "demand" in changes):
                            cancellation = await complete_write(restore_forecast_settings, changes)
                            if cancellation is not None:
                                raise cancellation
                return web.json_response(
                    {
                        **setup_definitions(),
                        "devices": catalogue.devices(installation_catalogue(installation, solar)),
                        "warnings": warnings(installation, solar),
                    }
                )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError) as error:
            raise web.HTTPServiceUnavailable(
                text=request_error_message(
                    error,
                    logger=_LOGGER,
                    operation="Installation settings save",
                    safe_messages=(),
                    fallback="Installation save could not be confirmed. Check the saved settings before retrying.",
                )
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
    app.router.add_put("/v1/setup", configure)
    app.router.add_post("/v1/setup/preview", preview)
    return catalogue
