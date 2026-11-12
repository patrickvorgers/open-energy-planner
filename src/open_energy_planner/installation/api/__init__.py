"""Authenticated native installation settings and capability catalogue, with app-owned persistence."""

from dataclasses import asdict
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.installation.storage import document, parse, sources


def warnings(store, solar):
    """Warn when fixed PV may exceed export capacity. A grid limit grants no equipment curtailment permission."""
    if store.current is None or solar.configuration is None:
        return []
    fixed = {source.inverter_id for source in solar.configuration.sources if not source.can_curtail}
    capacity = sum(
        inverter_item.ac_limit_w
        for inverter_item in solar.configuration.inverters
        if inverter_item.inverter_id in fixed
    )
    return ["fixed_pv_export_limit"] if store.current.export_limit_w < capacity else []


def catalogue(store, solar, snapshot=None):
    """Project configured properties and reserve status independently of commands.

    The optional snapshot supplies measured battery energy. Ignore it when
    current_at(UTC now) rejects its freshness or validity window. An absent,
    rejected or battery-free snapshot yields measurement_unavailable. Confirmed
    configuration properties remain readable without an active plan.
    """
    if store.current is None:
        return None
    if snapshot is not None and not snapshot.current_at(datetime.now(UTC)):
        snapshot = None
    measured = {
        battery.device_id: battery.battery
        for battery in getattr(getattr(snapshot, "problem", None), "batteries", ())
    }
    devices = [
        {
            "id": battery.device_id,
            "kind": "battery",
            "name": battery.device_id,
            "can_follow_grid": battery.can_follow_grid,
            "energy_entity": battery.energy_entity,
            "configured": True,
            "calibration": store.calibration.document(battery, datetime.now(UTC))
            if battery.calibration_enabled
            else {"status": "disabled"},
            "reserve_status": (
                "measurement_unavailable"
                if battery.device_id not in measured
                else "reserve_satisfied"
                if measured[battery.device_id].initial_kwh
                >= measured[battery.device_id].minimum_kwh
                else "recovery_required"
                if battery.recover_reserve
                else "below_reserve"
            ),
            "configuration": {
                **asdict(battery),
                "inverter": asdict(
                    next(
                        inverter_item
                        for inverter_item in store.current.inverters
                        if inverter_item.inverter_id == battery.inverter_id
                    )
                ),
            },
        }
        for battery in store.current.batteries
    ]
    if solar.configuration:
        devices.extend(
            {
                "id": source.source_id,
                "kind": "solar",
                "name": source.source_id,
                "energy_entity": source.entity_id,
                "configured": True,
                "can_curtail": source.can_curtail,
                "configuration": {
                    "source_id": source.source_id,
                    "entity_id": source.entity_id,
                    "inverter_id": source.inverter_id,
                    "capacity_w": source.capacity_w,
                    "orientation": None if source.panels else source.orientation,
                    "tilt": None if source.panels else source.tilt,
                    "measurement_boundary": source.measurement_boundary,
                    "can_curtail": source.can_curtail,
                    "inverter": asdict(
                        next(
                            inverter_item
                            for inverter_item in solar.configuration.inverters
                            if inverter_item.inverter_id == source.inverter_id
                        )
                    ),
                },
            }
            for source in solar.configuration.sources
        )
    return devices


def register(app, store, solar, live, lock, persist, snapshot, membership):
    """Register bounded compare-and-set settings without provider access or physical control."""

    async def get_settings(_request):
        """Return cached editable settings and explicit read faults."""
        async with lock:
            return web.json_response(
                {
                    "configuration": document(store.current),
                    "issue": store.issue,
                    "devices": membership.devices(
                        catalogue(store, solar, live.current_snapshot if live else None)
                    ),
                    "warnings": warnings(store, solar),
                }
            )

    def validate_links(candidate):
        """Reject contradictory converter copies or command identities before any settings are committed."""
        if solar.configuration:
            ids = {source.source_id for source in solar.configuration.sources}
            if ids & {battery.device_id for battery in candidate.batteries}:
                raise ValueError("PV and battery identities collide")
            converters = {
                inverter_item.inverter_id: inverter_item
                for inverter_item in solar.configuration.inverters
            }
            for inverter in candidate.inverters:
                if (
                    inverter.inverter_id in converters
                    and converters[inverter.inverter_id] != inverter
                ):
                    raise ValueError("Linked inverter definitions disagree")
                if (
                    not inverter.hybrid
                    and any(
                        source.inverter_id == inverter.inverter_id
                        for source in solar.configuration.sources
                    )
                    and any(
                        battery.inverter_id == inverter.inverter_id
                        for battery in candidate.batteries
                    )
                ):
                    raise ValueError("PV and storage sharing requires hybrid connection")

    async def preview(request):
        """Validate a prospective installation without changing forecasting or current settings."""
        try:
            candidate = parse(await request.json())
            async with lock:
                validate_links(candidate)
            return web.json_response({"configuration": document(candidate)})
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(
                text="Invalid installation settings or converter links"
            ) from None

    async def configure(request):
        """Install a fully persisted settings revision, then derive its matching measurement generation."""
        if live is None:
            raise web.HTTPConflict(text="Installation settings require Home Assistant mode")
        try:
            body = await request.json()
            candidate = parse(body)
            measurement = sources(candidate)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid installation settings") from None
        async with lock:
            try:
                membership.validate_membership("installation", candidate)
                validate_links(candidate)
            except ValueError:
                raise web.HTTPBadRequest(
                    text="Installation must match Energy sources and converter links"
                ) from None
            current = store.current.version if store.current else None
            if body.get("expected_version") != current:
                raise web.HTTPConflict(text="Installation changed. Reload before saving")
            if candidate.version != current:
                await persist(
                    store.save,
                    candidate,
                    lambda _value: live.install_configuration(measurement),
                    contract_input=True,
                )
        return await snapshot(request)

    app.router.add_get("/v1/installation", get_settings)
    app.router.add_post("/v1/installation/preview", preview)
    app.router.add_put("/v1/installation", configure)
