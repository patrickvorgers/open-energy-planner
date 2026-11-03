"""Current live installation fixtures with real HTTP, forecasts and isolated market evidence."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from tests.app.planning.test_live_app import TOKEN, Solver
from tests.app.solar.test_solar import SITE
from tests.database.support.archive import make_store


@asynccontextmanager
async def running_forecasts(path, *, weather_factory=None):
    """Run the current installation compiler with independently cached 48-hour market rates."""
    now = datetime.now(UTC)
    start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    series = PriceSeries(
        "fixture",
        "NL",
        "EUR",
        now,
        now,
        None,
        tuple(
            PricePoint(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                0.2,
                PriceKind.PUBLISHED,
                15,
            )
            for index in range(196)
        ),
    )
    store = make_store(path, PriceStore)
    store.save((series,))

    @asynccontextmanager
    async def prices(_database_engine):
        """Supply typed market evidence without starting external network providers."""
        yield PriceService("NL", None, None, store)

    options = {"solar_weather_factory": weather_factory} if weather_factory else {}
    runner = web.AppRunner(
        create_app(
            path, TOKEN, source="home_assistant", solver=Solver(), price_factory=prices, **options
        )
    )
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
            yield session, url
    finally:
        await runner.cleanup()


async def configure_forecasts(session, url, *, solar_definition=None):
    """Select a battery-free 5-kW installation and explicit no-PV configuration over HTTP."""
    solar_definition = solar_definition or {"sources": [], "inverters": []}
    async with session.put(url + "/v1/solar", json=solar_definition) as response:
        assert response.status == 200, await response.text()
    async with session.put(
        url + "/v1/installation",
        json={
            "grid_power_entity": "sensor.grid",
            "import_limit_w": 5000,
            "export_limit_w": 5000,
            "inverters": [],
            "batteries": [],
        },
    ) as response:
        assert response.status == 200, await response.text()
        state = await response.json()
    from tests.ha_contract.support.metadata import setup_metadata

    metadata = setup_metadata()
    metadata["entities"][1]["entity_id"] = "sensor.house"
    terms = [{"entity_id": "sensor.house", "sign": 1}]
    for source in solar_definition["sources"]:
        terms.append({"entity_id": source["entity_id"], "sign": 1})
        metadata["entities"].append(
            {
                "entity_id": source["entity_id"],
                "name": source["source_id"],
                "unit": "kWh",
                "device_id": source["source_id"],
                "device_class": "energy",
                "state_class": "total_increasing",
            }
        )
    metadata["suggestions"].update(
        terms=terms,
        solar=bool(solar_definition["sources"]),
        solar_sources=[source["entity_id"] for source in solar_definition["sources"]],
    )
    async with session.post(url + "/v1/ha/catalogue", json=metadata) as response:
        assert response.status == 200, await response.text()
    await observe_forecasts(session, url, state["measurement_config"])
    return state


async def observe_forecasts(session, url, configuration):
    """Submit only current grid evidence plus the real installation timezone/location."""
    now = datetime.now(UTC).isoformat()
    async with session.post(
        url + "/v1/observations",
        json={
            "version": now,
            "config_revision": configuration["revision"],
            "captured_at": now,
            "readings": [
                {
                    "key": "grid.power",
                    "entity_id": "sensor.grid",
                    "value": 0,
                    "unit": "W",
                    "observed_at": now,
                    "reported_at": now,
                    "timestamp_kind": "ha_state",
                }
            ],
            "issues": [],
            "location": asdict(SITE),
        },
    ) as response:
        assert response.status == 200, await response.text()
        return await response.json()
