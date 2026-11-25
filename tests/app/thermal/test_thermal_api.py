"""Exercise durable thermal settings, optimistic concurrency and peer authority over real HTTP."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
from aiohttp import ClientSession, Fingerprint

from open_energy_planner.app import create_app
from open_energy_planner.appliances.thermal.applications import attribute_history
from open_energy_planner.discovery import certificate, connection_app
from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint
from open_energy_planner.pairing import PAIRING_KEY
from tests.ha_contract.support.connection import server
from tests.ha_contract.support.metadata import setup_metadata

TOKEN = "thermal-admin-test-only-0123456789"
PEER_TOKEN = "thermal-peer-test-only-0123456789"
STORE = "00000000-0000-4000-8000-000000000101"


class NavigationLinks(HTMLParser):
    """Read rendered navigation destinations and current-page semantics without executing markup."""

    def __init__(self):
        """Collect each response independently so state cannot leak across page requests."""
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attributes):
        """Retain only actual anchor destinations and their accessibility current-page marker."""
        values = dict(attributes)
        if tag == "a" and "href" in values:
            self.links[values["href"]] = values.get("aria-current")


@pytest.mark.parametrize("trusted", [False, True])
def test_thermal_navigation_uses_only_the_trusted_ingress_prefix(tmp_path, trusted):
    """Every page links to the boiler inside HA ingress, and a spoofed header gains no trusted prefix."""

    async def scenario():
        """Parse actual HTTP-rendered shells and verify the thermal page's current-page semantics."""
        application = create_app(
            tmp_path,
            TOKEN,
            source="home_assistant",
            ingress_peer="127.0.0.1" if trusted else "172.30.32.2",
        )
        base = "/api/hassio_ingress/thermal_test"
        expected = (base if trusted else "") + "/thermal"
        async with server(application) as url, ClientSession() as client:
            for path in ("/setup", "/overview", "/contract", "/thermal"):
                async with client.get(url + path, headers={"X-Ingress-Path": base}) as response:
                    assert response.status == 200
                    parser = NavigationLinks()
                    parser.feed(await response.text())
                    assert expected in parser.links
                    assert (parser.links[expected] == "page") is (path == "/thermal")

    asyncio.run(scenario())


def catalogue():
    """Declare an Energy boiler meter with independent temperature and electrical power sensors."""
    document = setup_metadata()
    for entity, quantity, unit in (
        ("boiler_energy", "energy", "kWh"),
        ("boiler_temperature", "temperature", "°C"),
        ("boiler_power", "power", "W"),
    ):
        document["entities"].append(
            {
                "entity_id": "sensor." + entity,
                "name": entity,
                "device_name": "Utility boiler",
                "unit": unit,
                "device_id": "boiler",
                "device_class": quantity,
                "state_class": "total_increasing" if quantity == "energy" else "measurement",
            }
        )
    document["suggestions"]["consumers"] = [{"entity_id": "sensor.boiler_energy", "upstream": None}]
    return document


def configuration():
    """Use explicit declared physics and one thermal kWh at 07:00, never a learned draw profile."""
    return {
        "stores": [
            {
                "id": STORE,
                "energy_entity": "sensor.boiler_energy",
                "temperature_entity": "sensor.boiler_temperature",
                "power_entity": "sensor.boiler_power",
                "volume_litres": 120,
                "minimum_c": 45,
                "maximum_c": 65,
                "ambient_c": 20,
                "loss_w_per_c": 1.2,
                "heater_w": 2000,
                "draw_profile_kwh": [0] * 7 + [1] + [0] * 16,
                "enabled": True,
            }
        ]
    }


def test_shared_heat_source_keeps_one_weather_forecast_and_configured_boiler(tmp_path):
    """Share one meter across room and water without changing the configured boiler or meter total."""

    async def scenario():
        """Exercise versioned configuration, observed targets and mode reports over authenticated HTTP."""
        app = create_app(tmp_path, TOKEN, source="home_assistant")
        metadata = catalogue()
        metadata["areas"] = [
            {"id": "living", "name": "Living room"},
            {"id": "attic", "name": "Attic"},
            {"id": "hall", "name": "Hall"},
        ]
        quatt, airco, attic = "sensor.shared_heat", "sensor.room_airco", "sensor.attic_airco"
        thermostat = "climate.central"
        for identity, area in (
            (quatt, "hall"),
            (airco, "living"),
            (attic, "attic"),
            (thermostat, "hall"),
        ):
            climate = identity == thermostat
            metadata["entities"].append(
                {
                    "entity_id": identity,
                    "name": identity,
                    "area_id": area,
                    "device_id": "",
                    "unit": "" if climate else "kWh",
                    "device_class": "" if climate else "energy",
                    "state_class": "" if climate else "total_increasing",
                }
            )
            if not climate:
                metadata["suggestions"]["consumers"].append(
                    {"entity_id": identity, "upstream": None}
                )
        room_id = "00000000-0000-4000-8000-000000000201"
        attic_id = "00000000-0000-4000-8000-000000000202"
        tank_id = "00000000-0000-4000-8000-000000000203"
        physical = configuration()
        applications = [
            {
                "id": room_id,
                "kind": "space_heating",
                "area_id": "living",
                "name": "Saved room name",
                "climate_entity": thermostat,
                "maximum_preheat_c": 0.5,
                "sources": [
                    {"energy_entity": airco, "allow_scheduling": True},
                    {"energy_entity": quatt, "allow_scheduling": False},
                ],
            },
            {
                "id": attic_id,
                "kind": "space_heating",
                "area_id": "attic",
                "name": "Attic",
                "climate_entity": None,
                "maximum_preheat_c": None,
                "sources": [{"energy_entity": attic, "allow_scheduling": False}],
            },
            {
                "id": tank_id,
                "kind": "hot_water",
                "area_id": None,
                "name": "Hot water",
                "climate_entity": None,
                "maximum_preheat_c": None,
                "sources": [{"energy_entity": quatt, "allow_scheduling": False}],
            },
        ]
        authorization = {"Authorization": "Bearer " + TOKEN}
        async with server(app) as url, ClientSession(headers=authorization) as client:
            async with client.post(url + "/v1/ha/catalogue", json=metadata) as response:
                assert response.status == 200, await response.text()
            async with client.get(url + "/v1/thermal") as response:
                initial = await response.json()
            body = {
                "configuration": {**physical, "applications": applications},
                "expected_version": initial["configuration_version"],
            }
            async with client.post(url + "/v1/thermal/configuration", json=body) as response:
                assert response.status == 200, await response.text()
                saved = await response.json()
            assert saved["configuration"]["stores"] == physical["stores"]
            assert saved["applications"][0]["name"] == "Living room"
            assert set(saved["shared_sources"]) == {quatt}
            async with client.get(url + "/v1/consumers?view=settings") as response:
                consumers = await response.json()
                selected = consumers["selected"]
            assert selected == [
                {"entity_id": attic, "weather_sensitive": True},
                {"entity_id": "sensor.boiler_energy", "weather_sensitive": False},
                {"entity_id": airco, "weather_sensitive": True},
                {"entity_id": quatt, "weather_sensitive": True},
            ]
            assert consumers["thermal_meters"] == [attic, "sensor.boiler_energy", airco, quatt]
            duplicate = deepcopy(body)
            duplicate["expected_version"] = saved["configuration_version"]
            duplicate["configuration"]["applications"][1]["area_id"] = "living"
            async with client.post(url + "/v1/thermal/configuration", json=duplicate) as response:
                assert response.status == 400
                assert "only one space heating application" in (await response.text())
            for target, reporting_age in ((20, timedelta(minutes=30)), (21.5, timedelta())):
                now = datetime.now(UTC)
                observed_at = (now - reporting_age).isoformat()
                observation = {
                    "configuration_version": saved["configuration_version"],
                    "at": now.isoformat(),
                    "readings": [],
                    "climate": [
                        {
                            "id": room_id,
                            "entity_id": thermostat,
                            "temperature_c": 19.5,
                            "target_c": target,
                            "observed_at": observed_at,
                        }
                    ],
                }
                async with client.post(
                    url + "/v1/thermal/observations", json=observation
                ) as response:
                    assert response.status == 200, await response.text()
                    changed = await response.json()
                assert changed["applications"][0]["preheat_limit_c"] == target + 0.5
                assert changed["applications"][0]["actual"]["observed_at"] == observed_at
                assert changed["applications"][0]["actual"]["captured_at"] == now.isoformat()
                assert (
                    changed["applications"][0]["planning_status"]
                    == "Forecast only. Active planning requires one independent heat source."
                )
                assert changed["applications"][1]["planning_status"] == "Forecast only"
            async with client.post(
                url + "/v1/thermal/mode", json={"meter": quatt, "application_id": tank_id}
            ) as response:
                assert response.status == 200, await response.text()
                assert (await response.json())["source_modes"][quatt]["application_id"] == tank_id
            async with client.post(
                url + "/v1/thermal/mode", json={"meter": quatt, "application_id": attic_id}
            ) as response:
                assert response.status == 400
            metadata["suggestions"]["consumers"] = [
                source
                for source in metadata["suggestions"]["consumers"]
                if source["entity_id"] != attic
            ]
            async with client.post(url + "/v1/ha/catalogue", json=metadata) as response:
                assert response.status == 200, await response.text()
            detached = deepcopy(body)
            detached["expected_version"] = saved["configuration_version"]
            detached["configuration"]["applications"][1]["sources"] = []
            async with client.post(url + "/v1/thermal/configuration", json=detached) as response:
                assert response.status == 200, await response.text()
            async with client.get(url + "/v1/consumers?view=settings") as response:
                remaining = await response.json()
            assert attic not in remaining["thermal_meters"]
            assert attic not in {source["entity_id"] for source in remaining["selected"]}

    asyncio.run(scenario())


def test_shared_source_attribution_uses_only_complete_agreeing_mode_reports():
    """Preserve cumulative kWh once, keeping disconnected or ambiguous hours unknown."""
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    hour = timedelta(hours=1)
    history = CumulativeHistory(
        "sensor.heat",
        tuple(
            EnergyEndpoint(start + index * hour, value)
            for index, value in enumerate((10, 12, 15, 19))
        ),
    )
    reports = [
        {
            "observed_at": (start + hour + timedelta(minutes=minute)).isoformat(),
            "application_id": "water",
        }
        for minute in range(0, 60, 4)
    ]
    reports.append({"observed_at": (start + 2 * hour).isoformat(), "application_id": "room"})
    result = attribute_history(history, reports, {"room", "water"}, start, timedelta(minutes=5))
    assert result == {"applications_kwh": {"room": 0, "water": 2}, "unknown_kwh": 7}


def test_thermal_api_versioning_telemetry_and_peer_authority(tmp_path):
    """Only admin edits source bindings. A TLS peer reports measurements and bounded intent, never configuration."""

    async def scenario():
        """Persist real settings and reject stale writes without changing their confirmed version."""
        application = create_app(tmp_path, TOKEN, source="home_assistant")
        pairing = application[PAIRING_KEY]
        context, pin = certificate(tmp_path, pairing.instance_id)
        pairing.peer = {
            "peer_id": "thermal-ha-test-instance",
            "name": "Test HA",
            "token": PEER_TOKEN,
        }
        async with server(application) as url, ClientSession() as client:
            admin = {"Authorization": "Bearer " + TOKEN}
            for method, path in (
                ("GET", "/v1/thermal"),
                ("POST", "/v1/thermal/configuration"),
                ("POST", "/v1/thermal/observations"),
                ("POST", "/v1/thermal/intent"),
            ):
                async with client.request(method, url + path, json={}) as response:
                    assert response.status == 401
            async with client.post(
                url + "/v1/ha/catalogue", headers=admin, json=catalogue()
            ) as response:
                assert response.status == 200, await response.text()
            async with client.get(url + "/v1/thermal", headers=admin) as response:
                initial = await response.json()
                assert response.status == 200
                assert initial["configuration"] == {"stores": []}
            body = {
                "configuration": configuration(),
                "expected_version": initial["configuration_version"],
            }
            async with client.post(
                url + "/v1/thermal/configuration", headers=admin, json=body
            ) as response:
                assert response.status == 200, await response.text()
                saved = await response.json()
            assert saved["configuration"] == configuration()
            assert saved["devices"][0]["name"] == "Utility boiler"
            assert saved["devices"][0]["actual"] is None
            stale = deepcopy(body)
            stale["configuration"]["stores"][0]["heater_w"] = 3000
            async with client.post(
                url + "/v1/thermal/configuration", headers=admin, json=stale
            ) as response:
                assert response.status == 400
            async with client.get(url + "/v1/thermal", headers=admin) as response:
                assert (await response.json())["configuration_version"] == saved[
                    "configuration_version"
                ]
            gateway = connection_app(pairing, TOKEN, url, url + "/setup")
            async with server(gateway, context) as peer_url:
                peer = {"Authorization": "Bearer " + PEER_TOKEN}
                ssl = Fingerprint(bytes.fromhex(pin))
                async with client.post(
                    peer_url + "/v1/thermal/configuration", headers=peer, ssl=ssl, json=body
                ) as response:
                    assert response.status == 403
                now = datetime.now(UTC)
                report = {
                    "configuration_version": saved["configuration_version"],
                    "at": now.isoformat(),
                    "readings": [
                        {
                            "id": STORE,
                            "temperature_c": 50,
                            "power_w": 0,
                            "observed_at": now.isoformat(),
                        }
                    ],
                }
                async with client.post(
                    peer_url + "/v1/thermal/observations", headers=peer, ssl=ssl, json=report
                ) as response:
                    assert response.status == 200, await response.text()
                    measured = await response.json()
                assert measured["devices"][0]["actual"]["temperature_c"] == 50
                assert measured["devices"][0]["actual"]["power_w"] == 0
                assert set(measured["devices"][0]["actual"]) == {
                    "id",
                    "temperature_c",
                    "power_w",
                    "observed_at",
                }
                assert measured["configuration_version"] == saved["configuration_version"]
                unchanged = deepcopy(report)
                unchanged["readings"][0]["observed_at"] = (now - timedelta(hours=1)).isoformat()
                async with client.post(
                    peer_url + "/v1/thermal/observations", headers=peer, ssl=ssl, json=unchanged
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["devices"][0]["actual"]["temperature_c"] == 50
                expired = deepcopy(unchanged)
                expired["at"] = (now - timedelta(hours=1)).isoformat()
                async with client.post(
                    peer_url + "/v1/thermal/observations", headers=peer, ssl=ssl, json=expired
                ) as response:
                    assert response.status == 400
                for field, value, expected in (
                    ("enabled", False, 200),
                    ("minimum_c", 42, 200),
                    ("heater_w", 3000, 400),
                ):
                    async with client.post(
                        peer_url + "/v1/thermal/intent",
                        headers=peer,
                        ssl=ssl,
                        json={"device_id": STORE, "field": field, "value": value},
                    ) as response:
                        assert response.status == expected, await response.text()
                async with client.get(
                    peer_url + "/v1/ha/requests", headers=peer, ssl=ssl
                ) as response:
                    assert response.status == 200
                    metadata = await response.json()
                assert metadata["thermal"]["configuration"]["stores"][0]["enabled"] is False
                assert metadata["thermal"]["configuration"]["stores"][0]["minimum_c"] == 42
                assert metadata["thermal"]["configuration"]["stores"][0]["heater_w"] == 2000
            async with client.get(url + "/v1/thermal", headers=admin) as response:
                durable = (await response.json())["configuration"]
        restored = create_app(tmp_path, TOKEN, source="home_assistant")
        async with server(restored) as url, ClientSession(headers=admin) as client:
            async with client.get(url + "/v1/thermal") as response:
                document = await response.json()
            assert document["configuration"] == durable
            assert document["devices"][0]["actual"] is None

    asyncio.run(scenario())
