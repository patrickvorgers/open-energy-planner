"""Verify real integration identity, authenticated pairing and packaged independence."""

import asyncio
from uuid import uuid4

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession, web

from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import TOKEN, find, running_chain


@pytest.mark.parametrize(
    "explanation", [None, "", "missing", "Recorded savings evidence is invalid"]
)
def test_savings_warning_has_an_explanation_and_clears_after_recovery(explanation):
    """Real HA keeps unexplained failures visible with useful guidance and dismisses recovered warnings."""
    from unittest.mock import patch

    from homeassistant.components import persistent_notification

    async def scenario():
        """Exercise the authenticated snapshot and actual persistent-notification lifecycle."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot
            status = {"action_required": True}
            if explanation != "missing":
                status["issue"] = explanation

            async def with_savings_status():
                """Keep the real snapshot and substitute only the business status under test."""
                data = await snapshot()
                data["savings_status"] = dict(status)
                return data

            with (
                patch.object(coordinator.client, "snapshot", with_savings_status),
                patch.object(
                    persistent_notification,
                    "async_create",
                    wraps=persistent_notification.async_create,
                ) as notify,
                patch.object(
                    persistent_notification,
                    "async_dismiss",
                    wraps=persistent_notification.async_dismiss,
                ) as dismiss,
            ):
                await coordinator.sync()
                assert coordinator.issue == "savings_unavailable"
                message = notify.call_args.args[1]
                expected = (
                    explanation
                    if explanation not in (None, "", "missing")
                    else "No detailed settlement explanation is available"
                )
                assert expected in message
                assert "attention: None" not in message
                assert "Physical commands remain governed by their own plan validity" in message
                status.update(action_required=False, issue=None)
                await coordinator.sync()
                assert coordinator.issue is None
                dismiss.assert_called_with(hass, f"{DOMAIN}.{entry.entry_id}")

    asyncio.run(scenario())


def test_loaded_integration_reports_a_different_build_as_action_required(monkeypatch):
    """A real HA coordinator refuses a different app/integration build even when their release numbers agree."""
    from custom_components.open_energy_planner import client as client_module

    async def scenario():
        """Use the real app rejection and integration notification path instead of injecting a coordinator exception."""
        async with running_chain() as (_hass, entry, _url, _root):
            monkeypatch.setattr(
                client_module, "BUILD_INFO", {**client_module.BUILD_INFO, "build": "999.1"}
            )
            await entry.runtime_data.sync()
            assert entry.runtime_data.issue == "release_mismatch"

    asyncio.run(scenario())


def test_discovery_pairs_after_app_consent_and_migrates_existing_entry(tmp_path):
    """Actual HA discovery waits for app consent, then migrates the same entry to pinned TLS without losing entity identities."""
    from types import SimpleNamespace
    from urllib.parse import urlsplit

    from open_energy_planner.discovery import certificate, connection_app
    from open_energy_planner.pairing import Pairing

    async def scenario():
        """Use a real TLS listener and HA progress flow. No copied credential is submitted to the flow."""
        async with running_chain() as (hass, entry, upstream, _root):
            async with (
                ClientSession() as client,
                client.get(
                    upstream + "/v1/snapshot", headers={"Authorization": "Bearer " + TOKEN}
                ) as response,
            ):
                instance = (await response.json())["instance_id"]
            pairing = Pairing(tmp_path, instance)
            context, pin = certificate(tmp_path, instance)
            pairing.fingerprint = pin
            runner = web.AppRunner(connection_app(pairing, TOKEN, upstream, upstream + "/setup"))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
            try:
                port = runner.addresses[0][1]
                result = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "zeroconf"},
                    data=SimpleNamespace(
                        ip_address="127.0.0.1",
                        port=port,
                        properties={
                            "instance_id": instance,
                            "fingerprint": pin,
                            "release": "0.2.0",
                        },
                    ),
                )
                assert result["type"] == "progress"
                for _ in range(100):
                    if pairing.pending:
                        break
                    await asyncio.sleep(0.01)
                candidate = pairing.summary()["pending"][0]
                assert (
                    candidate["verification"] == result["description_placeholders"]["verification"]
                )
                assert "fingerprint" not in entry.data
                await pairing.approve(candidate["request_id"])
                for _ in range(700):
                    if entry.data.get("fingerprint"):
                        break
                    await asyncio.sleep(0.01)
                assert entry.data["fingerprint"] == pin
                assert urlsplit(entry.data["url"]).scheme == "https"
                assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                assert pairing.authorized("Bearer " + entry.data["token"])
                for _ in range(100):
                    if not any(
                        item["flow_id"] == result["flow_id"]
                        for item in hass.config_entries.flow.async_progress()
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert not any(
                    item["flow_id"] == result["flow_id"]
                    for item in hass.config_entries.flow.async_progress()
                )
                changed = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "zeroconf"},
                    data=SimpleNamespace(
                        ip_address="127.0.0.1",
                        port=port,
                        properties={
                            "instance_id": instance,
                            "fingerprint": "a" * 64,
                            "release": "0.2.0",
                        },
                    ),
                )
                assert changed["reason"] == "identity_changed"
                assert entry.data["fingerprint"] == pin
                previous_token = entry.data["token"]
                await pairing.reset()
                entry.async_start_reauth(hass)
                for _ in range(700):
                    if pairing.pending:
                        break
                    await asyncio.sleep(0.01)
                candidate = pairing.summary()["pending"][0]
                await pairing.approve(candidate["request_id"])
                for _ in range(400):
                    if entry.data["token"] != previous_token:
                        break
                    await asyncio.sleep(0.01)
                assert entry.data["token"] != previous_token
                assert entry.data["fingerprint"] == pin
                assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                assert pairing.authorized("Bearer " + entry.data["token"])
            finally:
                await runner.cleanup()

    asyncio.run(scenario())


def test_authenticated_http_rejects_missing_token_and_wrong_ack():
    """Call without credentials or acknowledge the wrong plan. Require authentication/conflict rejection."""

    async def scenario():
        """Exercise real HTTP authentication and reject acknowledgement of a different plan identity."""
        async with running_chain() as (_hass, _entry, url, _root), ClientSession() as session:
            async with session.get(url + "/v1/snapshot") as response:
                assert response.status == 401
            async with session.post(
                url + "/v1/ack",
                json={"plan_id": "wrong"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as response:
                assert response.status == 409

    asyncio.run(scenario())


def test_changed_certificate_requests_attention_without_replacing_the_trusted_identity(monkeypatch):
    """A TLS pin mismatch is actionable, never treated as a new trusted peer or an ordinary transient outage."""
    from aiohttp import ServerFingerprintMismatch

    async def scenario():
        """Inject a real transport error at the client boundary while retaining the previously valid command."""
        async with running_chain() as (hass, entry, _url, _root):

            async def mismatch():
                """Represent a server presenting a different certificate, without connecting to external hosts."""
                raise ServerFingerprintMismatch(b"x" * 32, b"y" * 32, "127.0.0.1", 8098)

            monkeypatch.setattr(entry.runtime_data.client, "snapshot", mismatch)
            await entry.runtime_data.sync()
            assert entry.runtime_data.issue == "identity_changed"
            assert find(hass, "battery_power_target").state == "-1000"

    asyncio.run(scenario())


def test_wrong_token_requests_user_attention_without_invalidating_valid_plan():
    """Reject authentication during a valid plan. Notify the user while retaining commands only within ordinary validity."""

    async def scenario():
        """Reject the app token and inspect the notification while the accepted plan remains locally valid."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator_item = entry.runtime_data
            coordinator_item.client.token = "wrong-test-token"
            await coordinator_item.sync()
            assert coordinator_item.issue == "authentication_failed"
            assert find(hass, "battery_power_target").state == "-1000"
            coordinator_item.client.token = TOKEN
            await coordinator_item.sync()
            assert coordinator_item.issue is None

    asyncio.run(scenario())


@pytest.mark.parametrize("existing", [True, False])
def test_authenticated_supervisor_bootstrap_migrates_without_user_steps(tmp_path, existing):
    """Real HA accepts a private admin bootstrap, retains its entry, and recovers revoked credentials without consent."""
    from types import SimpleNamespace

    from homeassistant.exceptions import Unauthorized

    from custom_components.open_energy_planner.supervisor import supervisor_link
    from open_energy_planner.discovery import certificate, connection_app
    from open_energy_planner.pairing import Pairing

    async def scenario():
        """Run the actual config flow and pinned TLS callback, rather than mocking a successful pairing response."""
        async with running_chain() as (hass, entry, upstream, _root):
            instance = entry.unique_id
            if not existing:
                await hass.config_entries.async_remove(entry.entry_id)
                await hass.async_block_till_done()
                assert not hass.config_entries.async_entries(DOMAIN)
            pairing = Pairing(tmp_path, instance)
            pairing.supervisor_secret = "private-authenticated-supervisor-proof"
            context, pin = certificate(tmp_path, pairing.instance_id)
            pairing.fingerprint = pin
            runner = web.AppRunner(connection_app(pairing, TOKEN, upstream, "/app/test_oep"))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
            try:
                result = []
                errors = []
                connection = SimpleNamespace(
                    user=SimpleNamespace(is_admin=True),
                    send_result=lambda *args: result.append(args),
                    send_error=lambda *args: errors.append(args),
                )
                body = {
                    "id": 1,
                    "type": "open_energy_planner/supervisor_link",
                    "host": "127.0.0.1",
                    "port": runner.addresses[0][1],
                    "fingerprint": pin,
                    "instance_id": pairing.instance_id,
                    "secret": pairing.supervisor_secret,
                }
                supervisor_link(hass, connection, body)
                for _ in range(200):
                    if result or errors:
                        break
                    await asyncio.sleep(0.01)
                await hass.async_block_till_done()
                assert result and not errors
                if not existing:
                    entry = hass.config_entries.async_entries(DOMAIN)[0]
                assert entry.data["supervisor"] is True
                for _ in range(100):
                    statuses = [
                        state
                        for state in hass.states.async_all()
                        if state.entity_id.endswith("open_energy_planner_status")
                    ]
                    if statuses and statuses[0].attributes.get("connection_type") == "supervisor":
                        break
                    await asyncio.sleep(0.01)
                assert statuses[0].attributes["connection_type"] == "supervisor"
                assert entry.data["fingerprint"] == pin
                assert entry.data["ui_url"] == "/app/test_oep"
                from homeassistant.helpers import device_registry as dr

                device = dr.async_get(hass).async_get_device_by_identifier(
                    (DOMAIN, f"{entry.entry_id}_ems"), entry.entry_id
                )
                assert device.configuration_url == "homeassistant://app/test_oep"
                assert pairing.authorized("Bearer " + entry.data["token"])
                assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                assert not pairing.pending
                previous = entry.data["token"]
                await pairing.reset()
                supervisor_link(hass, connection, {**body, "id": 2})
                for _ in range(200):
                    if len(result) >= 2 or errors:
                        break
                    await asyncio.sleep(0.01)
                await hass.async_block_till_done()
                assert entry.data["token"] != previous
                assert pairing.authorized("Bearer " + entry.data["token"])
                assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                supervisor_link(hass, connection, {**body, "id": 3, "secret": "wrong"})
                for _ in range(200):
                    if errors:
                        break
                    await asyncio.sleep(0.01)
                assert errors[-1][1] == "link_failed"
                connection.user.is_admin = False
                with pytest.raises(Unauthorized):
                    supervisor_link(hass, connection, {**body, "id": 4})
            finally:
                await runner.cleanup()

    asyncio.run(scenario())


def test_distributed_integration_imports_in_isolated_python_without_app_package(
    tmp_path, monkeypatch
):
    """Every runtime integration module imports from the generated ZIP with Python isolation and no app source on its path."""
    import subprocess
    import sys
    import zipfile

    from script.release import package_ha_app
    from script.release.package_release import build

    # The HA test image contains approved source files, without Git metadata.
    # Ignored-input admission is covered by the independent packaging tests.
    monkeypatch.setattr(package_ha_app, "reject_ignored", lambda root, paths: None)
    destination = tmp_path / "bundle"
    build(destination, revision="a" * 40)
    component = tmp_path / "ha/custom_components/open_energy_planner"
    component.mkdir(parents=True)
    with zipfile.ZipFile(destination / "open_energy_planner.zip") as archive:
        archive.extractall(component)
    code = """
import importlib, pathlib, pkgutil, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
package = importlib.import_module('custom_components.open_energy_planner')
for item in pkgutil.walk_packages(package.__path__, package.__name__ + '.'):
    module = importlib.import_module(item.name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
assert importlib.util.find_spec('open_energy_planner') is None
print('Self-contained integration package imports passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path / "ha")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_hassio_metadata_starts_authenticated_bootstrap_without_pairing(monkeypatch):
    """Untrusted Supervisor discovery metadata starts bootstrap and cannot itself authorize a connection."""
    from homeassistant.helpers.service_info.hassio import HassioServiceInfo

    async def scenario():
        """Run the real Hassio flow and observe delegation without submitting an administrator secret."""
        async with running_chain() as (hass, entry, _url, _root):
            registrations = []
            monkeypatch.setattr(
                "custom_components.open_energy_planner.supervisor.register",
                registrations.append,
            )
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "hassio"},
                data=HassioServiceInfo(
                    config={"host": "127.0.0.1", "port": 8098},
                    name="OEP test app",
                    slug="test_oep",
                    uuid=uuid4().hex,
                ),
            )
            assert result["type"] == "abort"
            assert result["reason"] == "supervisor_connecting"
            assert registrations == [hass]
            assert hass.config_entries.async_entries(DOMAIN) == [entry]

    asyncio.run(scenario())


def test_manual_form_explains_automatic_ha_os_connection_without_changing_fallback():
    """The actual HA form labels manual setup as advanced and explains the token-free HA OS route."""
    from homeassistant.helpers.translation import async_get_translations

    async def scenario():
        """Load native flow metadata and its frontend translations while preserving existing HA ownership."""
        async with running_chain(fixture_devices=False) as (hass, entry, _url, _root):
            before = dict(entry.data)
            result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
            assert result["type"] == "form" and result["step_id"] == "user"
            assert {str(field) for field in result["data_schema"].schema} == {"url", "token"}
            translations = await async_get_translations(hass, "en", "config", {DOMAIN})
            prefix = "component.open_energy_planner.config."
            assert translations[prefix + "step.user.title"] == "Advanced manual connection"
            description = translations[prefix + "step.user.description"]
            assert "cancel this dialog" in description
            assert "Settings > Apps" in description
            assert "without an app URL or API token" in description
            assert "advanced standalone connection" in description
            assert hass.config_entries.async_entries(DOMAIN) == [entry]
            assert entry.data == before
            hass.config_entries.flow.async_abort(result["flow_id"])

    asyncio.run(scenario())
