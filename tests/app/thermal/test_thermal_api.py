"""Exercise durable thermal settings, optimistic concurrency and peer authority over real HTTP."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
from aiohttp import ClientSession, Fingerprint

from open_energy_planner.app import create_app
from open_energy_planner.discovery import certificate, connection_app
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
                async with client.get(peer_url + "/v1/snapshot", headers=peer, ssl=ssl) as response:
                    assert response.status == 200
                    snapshot = await response.json()
                assert snapshot["thermal"]["configuration"]["stores"][0]["enabled"] is False
                assert snapshot["thermal"]["configuration"]["stores"][0]["minimum_c"] == 42
                assert snapshot["thermal"]["configuration"]["stores"][0]["heater_w"] == 2000
            async with client.get(url + "/v1/thermal", headers=admin) as response:
                durable = (await response.json())["configuration"]
        restored = create_app(tmp_path, TOKEN, source="home_assistant")
        async with server(restored) as url, ClientSession(headers=admin) as client:
            async with client.get(url + "/v1/thermal") as response:
                document = await response.json()
            assert document["configuration"] == durable
            assert document["devices"][0]["actual"] is None

    asyncio.run(scenario())
