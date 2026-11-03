"""Independent consent, credential ownership, TLS identity and restart checks for local pairing."""

import asyncio
import hashlib
import json

import pytest
from aiohttp import ClientSession, Fingerprint, web

from open_energy_planner.app import create_app
from open_energy_planner.discovery import certificate, connection_app
from open_energy_planner.pairing import PAIRING_KEY, Pairing
from tests.ha_contract.support.connection import PEER, SECRET, TOKEN, server


def test_consent_is_required_claim_is_bound_and_restart_retains_peer(tmp_path):
    """Discovery/request cannot authorize commands. Only app consent enables the requesting secret, retained on restart."""

    async def scenario():
        """Check state boundaries independently of the HA client and prevent duplicate installation ownership."""
        pairing = Pairing(tmp_path, "test-instance")
        pairing.fingerprint = "a" * 64
        request = pairing.request(PEER)
        claim = {"request_id": request["request_id"], "secret": SECRET}
        assert pairing.claim(claim) == {"status": "pending"}
        assert not pairing.authorized("Bearer " + SECRET)
        with pytest.raises(web.HTTPNotFound):
            pairing.claim({**claim, "secret": "wrong"})
        summary = pairing.summary()
        expected = hashlib.sha256(
            (hashlib.sha256(SECRET.encode()).hexdigest() + "a" * 64).encode()
        ).hexdigest()[:16]
        assert summary["pending"][0]["verification"] == expected
        assert SECRET not in str(summary)
        await pairing.approve(request["request_id"])
        token = pairing.claim(claim)["token"]
        assert token != SECRET and len(token) >= 48
        assert pairing.authorized("Bearer " + token)
        restarted = Pairing(tmp_path, "test-instance")
        assert restarted.authorized("Bearer " + token)
        assert restarted.pending == {}
        with pytest.raises(web.HTTPConflict):
            pairing.request(PEER)
        await pairing.reset()
        assert not pairing.authorized("Bearer " + token)
        assert Pairing(tmp_path, "test-instance").peer is None

    asyncio.run(scenario())


def test_expired_and_unselected_candidates_cannot_claim(tmp_path):
    """An expired request cannot be approved. Choosing one candidate removes all competing claim paths."""

    async def scenario():
        """Expire using the monotonic deadline rather than sleeping through the five-minute window."""
        pairing = Pairing(tmp_path, "test-instance")
        pairing.fingerprint = "a" * 64
        expired = pairing.request(PEER)["request_id"]
        pairing.pending[expired]["expires"] = 0
        with pytest.raises(web.HTTPConflict):
            await pairing.approve(expired)
        first = pairing.request(PEER)["request_id"]
        renewed = pairing.request(PEER)["request_id"]
        assert first not in pairing.pending
        first = renewed
        other = pairing.request({**PEER, "peer_id": "other-home-assistant-12345"})["request_id"]
        await pairing.approve(first)
        with pytest.raises(web.HTTPNotFound):
            pairing.claim({"request_id": other, "secret": SECRET})

    asyncio.run(scenario())


def test_tls_pinning_consent_and_admin_separation_over_real_http(tmp_path):
    """TLS peers cannot approve themselves. An admin-confirmed credential can access plans, never consent endpoints."""

    async def scenario():
        """Exercise the actual app middleware and TLS gateway, including wrong certificate and plaintext rejection."""
        app = create_app(tmp_path, TOKEN, source="home_assistant")
        pairing = app[PAIRING_KEY]
        context, pin = certificate(tmp_path, pairing.instance_id)
        assert certificate(tmp_path, pairing.instance_id)[1] == pin
        pairing.fingerprint = pin
        async with server(app) as upstream:
            gateway = connection_app(pairing, TOKEN, upstream, upstream + "/setup")
            async with server(gateway, context) as url, ClientSession() as client:
                fingerprint = Fingerprint(bytes.fromhex(pin))
                with pytest.raises(Exception) as error:
                    await client.get(url + "/pair/info", ssl=Fingerprint(b"x" * 32))
                assert "Fingerprint" in type(error.value).__name__
                async with client.post(
                    url + "/pair/request", json=PEER, ssl=fingerprint
                ) as response:
                    assert response.status == 200
                    pending = await response.json()
                async with client.post(
                    upstream + "/v1/pairing/approve", json={"request_id": pending["request_id"]}
                ) as response:
                    assert response.status == 401
                async with client.post(
                    upstream + "/v1/pairing/approve",
                    json={"request_id": pending["request_id"]},
                    headers={"Authorization": "Bearer " + TOKEN},
                ) as response:
                    assert response.status == 200
                async with client.post(
                    url + "/pair/claim",
                    json={"request_id": pending["request_id"], "secret": SECRET},
                    ssl=fingerprint,
                ) as response:
                    token = (await response.json())["token"]
                async with client.get(
                    url + "/v1/snapshot",
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["instance_id"] == pairing.instance_id
                for route, body_mib, expected_status in (
                    ("/v1/history", 2, 409),
                    ("/v1/observations", 2, 413),
                    ("/v1/history", 17, 413),
                ):
                    async with client.post(
                        url + route,
                        json={
                            "request_version": "not-current",
                            "padding": "x" * (body_mib * 1024 * 1024),
                        },
                        ssl=fingerprint,
                        headers={"Authorization": "Bearer " + token},
                    ) as response:
                        assert response.status == expected_status
                        if expected_status == 409:
                            assert "History request changed" in await response.text()
                annual_report = {
                    "request_version": "not-current",
                    "statistics": [
                        {
                            "statistic_id": f"sensor.export_{meter_index}",
                            "metadata_id": meter_index + 1,
                            "source": "recorder",
                            "unit": "kWh",
                            "hours": [
                                [
                                    1704063600.0 + hour_index * 3600,
                                    0.00012345678901234567,
                                    0.00012345678901234567,
                                ]
                                for hour_index in range(8785)
                            ],
                        }
                        for meter_index in range(16)
                    ],
                }
                # A leap year plus its boundary row must reach upstream even above 8 MiB.
                assert len(json.dumps(annual_report).encode()) > 8 * 1024 * 1024
                async with client.post(
                    url + "/v1/history",
                    json=annual_report,
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 409
                    assert "History request changed" in await response.text()
                for operation in (
                    "tasks",
                    "cycles",
                    "claim",
                    "feedback",
                    "deadline",
                    "deadline_field",
                    "cancel",
                ):
                    async with client.post(
                        url + "/v1/appliances/" + operation,
                        json={},
                        ssl=fingerprint,
                        headers={"Authorization": "Bearer " + token},
                    ) as response:
                        assert response.status == 400, (
                            "Confirmed peer reaches validated appliance operation"
                        )
                for operation in ("observations", "assign", "intent"):
                    async with client.post(
                        url + "/v1/ev/" + operation,
                        json=[],
                        ssl=fingerprint,
                        headers={"Authorization": "Bearer " + token},
                    ) as response:
                        assert response.status == 400, "Peer reaches validated EV operation"
                async with client.post(
                    url + "/v1/forecast-quality/history",
                    json=[],
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 400, "Peer reaches validated Recorder receipt"
                async with client.get(
                    url + "/v1/forecast-quality/original",
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 403, "Quality archive reads remain app-owned"
                async with client.post(
                    url + "/v1/ev/configuration",
                    json={},
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 403, "EV configuration remains app-owned"
                for operation in ("device",):
                    async with client.post(
                        url + "/v1/appliances/" + operation,
                        json={},
                        ssl=fingerprint,
                        headers={"Authorization": "Bearer " + token},
                    ) as response:
                        assert response.status == 403, (
                            "Peer cannot change app-owned permissions/configuration"
                        )
                async with client.post(
                    url + "/v1/pairing/approve",
                    json={},
                    ssl=fingerprint,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    assert response.status == 403
            async with (
                server(connection_app(pairing, TOKEN, upstream, upstream)) as plaintext,
                ClientSession() as client,
                client.get(plaintext + "/pair/info") as response,
            ):
                assert response.status == 403

    asyncio.run(scenario())


def test_optional_address_discovery_failure_keeps_app_available(tmp_path, monkeypatch):
    """An unavailable multicast route must not take down the app UI or authenticated API."""
    import socket

    def unavailable(_name):
        """Simulate hostname resolution failure before optional multicast advertising."""
        raise OSError("No local address")

    async def scenario():
        """Start the actual app lifecycle with a real TLS listener despite broken advertisement setup."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr(socket, "gethostbyname", unavailable)
        app = create_app(tmp_path, TOKEN, source="home_assistant", discovery_options={"port": 0})
        async with server(app) as url, ClientSession() as client:
            async with client.get(url + "/setup") as response:
                assert response.status == 200
            async with client.get(
                url + "/v1/snapshot", headers={"Authorization": "Bearer " + TOKEN}
            ) as response:
                assert response.status == 200

    asyncio.run(scenario())


def test_malformed_json_secrets_are_rejected_over_pinned_tls(tmp_path):
    """Unauthenticated lone-surrogate JSON proofs must return 403/400 without server errors or peer ownership."""

    async def scenario():
        """Exercise actual JSON decoding on the TLS gateway for Supervisor, standalone request and claim routes."""
        pairing = Pairing(tmp_path, "malformed-proof-test")
        pairing.supervisor_secret = SECRET
        context, pin = certificate(tmp_path, pairing.instance_id)
        pairing.fingerprint = pin
        async with (
            server(connection_app(pairing, TOKEN, "http://127.0.0.1:1", "/setup"), context) as url,
            ClientSession() as client,
        ):
            for path, body, expected in (
                ("/pair/supervisor", {**PEER, "secret": "\ud800"}, 403),
                ("/pair/request", {**PEER, "secret": "\ud800" * 24}, 400),
                ("/pair/claim", {"request_id": "missing", "secret": "\ud800"}, 400),
            ):
                async with client.post(
                    url + path, json=body, ssl=Fingerprint(bytes.fromhex(pin))
                ) as response:
                    assert response.status == expected
            assert pairing.peer is None and not pairing.pending

    asyncio.run(scenario())
