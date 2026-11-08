"""HA panel ownership changes through actual discovery, persistence and authenticated configuration routes."""

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarInverter,
    SolarSource,
)
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import document
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.database.support.backends import BACKENDS
from tests.ha_contract.support.metadata import setup_metadata

TOKEN = "panel-following-test-token-0123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def panel_database(request, tmp_path):
    """Own a fresh shared-schema database and dispose it after the app closes on every backend."""
    _backend, url = request.param
    engine = archive_engine(tmp_path, url)
    SCHEMA.drop_all(engine)
    try:
        yield url
    finally:
        SCHEMA.drop_all(engine)
        engine.dispose()


def manual_configuration():
    """Use explicit west-facing manual geometry and an independently rated shared converter."""
    return SolarConfiguration(
        (SolarSource("array", "sensor.array", "converter", 3000, "west", tilt=35),),
        (SolarInverter("converter", 2500, efficiency=0.93, hybrid=True),),
    )


def discovery(*, issue=None, angle=95, linked=True):
    """Supply two linked planes whose forecast limit must never replace the physical 2.5 kW rating."""
    body = setup_metadata()
    body["entities"].append(
        {
            "entity_id": "sensor.array",
            "name": "Garden array",
            "unit": "kWh",
            "device_id": "array-meter",
            "device_class": "energy",
            "state_class": "total_increasing",
        }
    )
    body["suggestions"]["terms"].append({"entity_id": "sensor.array", "sign": 1})
    body["suggestions"].update(solar=True, solar_sources=["sensor.array"])
    body["suggestions"]["solar_configurations"] = [
        {
            "entity_id": "sensor.array",
            "issue": issue if linked else "solar_configuration_not_available",
            "providers": []
            if issue or not linked
            else [
                {
                    "adapter": "forecast_solar",
                    "entry_id": "garden",
                    "inverter_limit_w": 9900,
                    "panels": [
                        {
                            "panel_id": "garden.east",
                            "name": "East",
                            "capacity_w": 1400,
                            "azimuth": angle,
                            "tilt": 25,
                        },
                        {
                            "panel_id": "garden.west",
                            "name": "West",
                            "capacity_w": 1800,
                            "azimuth": 275,
                            "tilt": 40,
                        },
                    ],
                }
            ],
        }
    ]
    return body


async def settings(client):
    """Read committed settings and their independently presented ownership through the actual API."""
    response = await client.get("/v1/setup", headers=HEADERS)
    assert response.status == 200
    return await response.json()


async def upload(client, body):
    """Submit one complete synthetic catalogue without bypassing its real validation or reconciliation."""
    response = await client.post("/v1/ha/catalogue", headers=HEADERS, json=body)
    assert response.status == 200, await response.text()
    return await settings(client)


def test_follow_update_outage_unlink_and_relink_preserve_converter_and_originals(
    tmp_path, panel_database
):
    """Complete HA updates replace geometry, unknown reads retain it, and only explicit unlink enables manual writes."""
    original = manual_configuration()
    with closing(SqlPlanRepository(tmp_path, database_url=panel_database)) as owner:
        SolarStore(SqlDocuments(owner.engine, "solar")).save(original)

    async def scenario():
        """Exercise metadata and configuration transport with real persistence on each backend."""
        async with TestClient(
            TestServer(
                create_app(tmp_path, TOKEN, source="home_assistant", database_url=panel_database)
            )
        ) as client:
            followed = await upload(client, discovery())
            source = followed["solar"]["sources"][0]
            assert source["capacity_w"] == 3200
            assert [(panel["azimuth"], panel["tilt"]) for panel in source["panels"]] == [
                (95, 25),
                (275, 40),
            ]
            assert followed["solar"]["inverters"] == document(original)["inverters"]
            assert followed["panel_settings"]["sensor.array"] == {
                "state": "following",
                "managed": True,
            }
            unchanged = await upload(client, discovery())
            assert unchanged["solar"]["version"] == followed["solar"]["version"]
            updated = await upload(client, discovery(angle=110))
            assert updated["solar"]["version"] != followed["solar"]["version"]
            assert updated["solar"]["sources"][0]["panels"][0]["azimuth"] == 110
            tampered = deepcopy(updated["solar"])
            tampered["expected_version"] = tampered.pop("version")
            tampered["sources"][0]["panels"][0]["azimuth"] = 180
            response = await client.put("/v1/solar", headers=HEADERS, json=tampered)
            assert response.status == 400
            assert "managed in Home Assistant" in await response.text()
            for issue in (
                "solar_configuration_incomplete",
                "solar_configuration_unsupported",
                "solar_configuration_ambiguous",
            ):
                missing = await upload(client, discovery(issue=issue))
                assert missing["solar"] == updated["solar"]
                assert missing["panel_settings"]["sensor.array"] == {
                    "state": "unavailable",
                    "managed": True,
                }
            omitted = discovery()
            omitted["suggestions"].pop("solar_configurations")
            missing = await upload(client, omitted)
            assert missing["solar"] == updated["solar"]
            assert missing["panel_settings"]["sensor.array"]["managed"] is True
            released = await upload(client, discovery(linked=False))
            assert (
                released["solar"]["sources"][0]["panels"]
                == updated["solar"]["sources"][0]["panels"]
            )
            assert released["solar"]["sources"][0]["ha_imports"] == []
            assert released["panel_settings"]["sensor.array"] == {
                "state": "manual",
                "managed": False,
            }
            manual = deepcopy(released["solar"])
            manual["expected_version"] = manual.pop("version")
            manual["sources"][0]["panels"][0]["azimuth"] = 125
            response = await client.put("/v1/solar", headers=HEADERS, json=manual)
            assert response.status == 200, await response.text()
            relinked = await upload(client, discovery(angle=80))
            assert relinked["solar"]["sources"][0]["panels"][0]["azimuth"] == 80
            assert relinked["solar"]["inverters"] == document(original)["inverters"]
            notices = await client.get("/v1/notifications", headers=HEADERS)
            assert notices.status == 200
            payload = await notices.json()
            following = next(
                notice
                for notice in payload["notifications"]
                if notice["code"] == "solar_panel_settings_following"
            )
            assert following["message"] == (
                "Home Assistant panel settings are now followed. "
                "Previous manual settings: "
                "1400 Wp at 125° with 25° tilt, 1800 Wp at 275° with 40° tilt. "
                "Current settings: "
                "1400 Wp at 80° with 25° tilt, 1800 Wp at 275° with 40° tilt."
            )
            assert following["auto_clear_minutes"] == 5
        async with TestClient(
            TestServer(
                create_app(tmp_path, TOKEN, source="home_assistant", database_url=panel_database)
            )
        ) as client:
            retained = await settings(client)
            assert retained["solar"]["sources"][0]["panels"][0]["azimuth"] == 80
            assert retained["panel_settings"]["sensor.array"] == {
                "state": "unavailable",
                "managed": True,
            }
        with closing(SqlPlanRepository(tmp_path, database_url=panel_database)) as owner:
            store = SolarStore(SqlDocuments(owner.engine, "solar"))
            assert store.current.inverters == original.inverters
            archived = store.documents.read(f"configuration-{original.version}.json")
            assert archived == {**document(original), "version": original.version}

    asyncio.run(scenario())


def test_cancelled_acknowledged_unlink_still_announces_manual_ownership(tmp_path, panel_database):
    """Cancellation after acknowledged persistence keeps both the committed unlink and its informational notice."""
    with closing(SqlPlanRepository(tmp_path, database_url=panel_database)) as owner:
        SolarStore(SqlDocuments(owner.engine, "solar")).save(manual_configuration())

    async def scenario():
        """Cancel at the persistence return boundary while retaining real storage and runtime installation."""
        app = create_app(tmp_path, TOKEN, source="home_assistant", database_url=panel_database)
        handler = next(
            route.handler.__self__
            for route in app.router.routes()
            if route.method == "POST" and route.resource.canonical == "/v1/ha/catalogue"
        )
        async with TestClient(TestServer(app)) as client:
            await upload(client, discovery())
            handler.catalogue.receive(discovery(linked=False), datetime.now(UTC))
            persist = handler.persist

            async def cancel_after_acceptance(*arguments, **options):
                """Model a cancellation that the drained writer propagates after installing its commit."""
                await persist(*arguments, **options)
                raise asyncio.CancelledError()

            handler.persist = cancel_after_acceptance
            async with handler.lock:
                with pytest.raises(asyncio.CancelledError):
                    await handler.reconcile()
            current = await settings(client)
            assert current["solar"]["sources"][0]["ha_imports"] == []
            assert current["panel_settings"]["sensor.array"] == {
                "state": "manual",
                "managed": False,
            }
            notices = await client.get("/v1/notifications", headers=HEADERS)
            payload = await notices.json()
            manual_notice = next(
                notice
                for notice in payload["notifications"]
                if notice["code"] == "solar_panel_settings_manual"
            )
            assert manual_notice["auto_clear_minutes"] == 5

    asyncio.run(scenario())


def test_unavailable_first_link_does_not_invent_panel_geometry(tmp_path, panel_database):
    """A new unreadable link has no accepted panel settings and cannot be bypassed with manual defaults."""

    async def scenario():
        """Keep a missing installation explicit through real discovery and attempted solar writes."""
        app = create_app(tmp_path, TOKEN, source="home_assistant", database_url=panel_database)
        handler = next(
            route.handler.__self__
            for route in app.router.routes()
            if route.method == "POST" and route.resource.canonical == "/v1/ha/catalogue"
        )
        async with TestClient(TestServer(app)) as client:
            candidate = document(manual_configuration())
            candidate["expected_version"] = None
            response = await client.put("/v1/solar", headers=HEADERS, json=candidate)
            assert response.status == 400
            assert (await settings(client))["solar"] is None
            current = await upload(client, discovery(issue="solar_configuration_incomplete"))
            assert current["solar"] is None
            assert current["panel_settings"]["sensor.array"] == {
                "state": "unavailable",
                "managed": True,
            }
            response = await client.put("/v1/solar", headers=HEADERS, json=candidate)
            assert response.status == 400
            assert "Wait for complete Home Assistant panel settings" in await response.text()
            assert (await settings(client))["solar"] is None
            handler.catalogue.updated -= timedelta(minutes=16)
            confirmed = await settings(client)
            assert confirmed["catalogue"]["stale"] is False
            assert confirmed["solar"] is None
            assert confirmed["panel_settings"]["sensor.array"] == {
                "state": "unavailable",
                "managed": True,
            }
            handler.catalogue.confirmed -= timedelta(minutes=16)
            stale = await settings(client)
            assert stale["catalogue"]["stale"] is True
            assert stale["panel_settings"]["sensor.array"] == {
                "state": "unavailable",
                "managed": True,
            }
            response = await client.put("/v1/solar", headers=HEADERS, json=candidate)
            assert response.status == 400
            assert (await settings(client))["solar"] is None

    asyncio.run(scenario())


def test_failed_unlink_and_malformed_discovery_do_not_release_ownership(
    tmp_path, panel_database, monkeypatch
):
    """A failed durable unlink and a malformed replacement retain accepted geometry and HA ownership."""
    with closing(SqlPlanRepository(tmp_path, database_url=panel_database)) as owner:
        SolarStore(SqlDocuments(owner.engine, "solar")).save(manual_configuration())

    async def scenario():
        """Use real requests and reject the store write before its acknowledged configuration can change."""
        async with TestClient(
            TestServer(
                create_app(tmp_path, TOKEN, source="home_assistant", database_url=panel_database)
            )
        ) as client:
            accepted = await upload(client, discovery())
            malformed = discovery(angle=999)
            response = await client.post("/v1/ha/catalogue", headers=HEADERS, json=malformed)
            assert response.status == 400
            assert (await settings(client))["solar"] == accepted["solar"]
            notices = await client.get("/v1/notifications", headers=HEADERS)
            original_notices = await notices.json()

            def fail_save(_store, _candidate):
                """Simulate unavailable storage without changing either the archive or runtime settings."""
                raise OSError("synthetic panel storage unavailable")

            with monkeypatch.context() as failure:
                failure.setattr(SolarStore, "save", fail_save)
                response = await client.post(
                    "/v1/ha/catalogue", headers=HEADERS, json=discovery(linked=False)
                )
                assert response.status == 500
                retained = await settings(client)
                assert retained["solar"] == accepted["solar"]
                assert retained["panel_settings"]["sensor.array"] == {
                    "state": "unavailable",
                    "managed": True,
                }
                notices = await client.get("/v1/notifications", headers=HEADERS)
                text = await notices.text()
                assert "solar_panel_settings_manual" not in text
                assert "Garden array" in text
                assert original_notices
            recovered = await upload(client, discovery(linked=False))
            assert recovered["panel_settings"]["sensor.array"] == {
                "state": "manual",
                "managed": False,
            }

    asyncio.run(scenario())
