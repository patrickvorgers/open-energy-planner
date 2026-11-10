"""App setup security, complete preview and bounded read-only HA discovery contracts."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.home_assistant.catalogue import HACatalogue
from tests.database.support.archive import list_documents, read_plan
from tests.ha_contract.support.metadata import setup_metadata


@pytest.mark.parametrize("field,value", [("integration_id", ""), ("integration_name", "x" * 161)])
def test_catalogue_rejects_incomplete_or_oversized_integration_metadata(field, value):
    """Integration labels and identities retain the same bounded, paired metadata contract."""
    body = setup_metadata()
    body["entities"][0].update(integration_id="entry", integration_name="Family car")
    body["entities"][0][field] = value
    with pytest.raises(ValueError, match="Invalid setup entity metadata"):
        HACatalogue().receive(body, datetime.now(UTC))


def test_catalogue_retains_integration_identity_and_title_without_provider_attributes():
    """The validated app catalogue preserves the integration grouping needed by source pickers."""
    body = setup_metadata()
    body["entities"][0].update(integration_id="entry", integration_name="Family car")
    catalogue = HACatalogue()
    catalogue.receive(body, datetime.now(UTC))
    assert catalogue.value["entities"][0]["integration_id"] == "entry"
    assert catalogue.value["entities"][0]["integration_name"] == "Family car"


def test_catalogue_separates_integration_type_account_and_service_device():
    """Bounded public type labels and service classification survive without replacing the account identity."""
    body = setup_metadata()
    body["entities"][0].update(
        integration_id="account-a",
        integration_name="Synthetic account",
        integration_domain="mqtt",
        integration_display_name="MQTT",
        device_entry_type="service",
    )
    catalogue = HACatalogue()
    catalogue.receive(body, datetime.now(UTC))
    entity = catalogue.value["entities"][0]
    assert entity["integration_id"] == "account-a"
    assert entity["integration_name"] == "Synthetic account"
    assert entity["integration_domain"] == "mqtt"
    assert entity["integration_display_name"] == "MQTT"
    assert entity["device_entry_type"] == "service"


@pytest.mark.parametrize(
    "metadata",
    [
        {"integration_domain": "mqtt"},
        {"integration_display_name": "MQTT"},
        {"integration_domain": "", "integration_display_name": "MQTT"},
        {"integration_domain": "mqtt", "integration_display_name": " "},
        {"integration_domain": "mqtt", "integration_display_name": "x" * 161},
        {"integration_domain": "x" * 161, "integration_display_name": "MQTT"},
        {"integration_domain": "mqtt", "integration_display_name": {"private": "data"}},
        {"device_entry_type": "vehicle"},
        {"device_entry_type": True},
    ],
)
def test_catalogue_rejects_invalid_integration_type_and_device_metadata(metadata):
    """Incomplete, oversized, arbitrary-object or invented device classifications fail before catalogue publication."""
    body = setup_metadata()
    body["entities"][0].update(metadata)
    catalogue = HACatalogue()
    with pytest.raises(ValueError, match="Invalid setup entity metadata"):
        catalogue.receive(body, datetime.now(UTC))
    assert catalogue.value is None


@pytest.mark.parametrize("write_fails", [False, True])
def test_catalogue_cancelled_source_write_still_invalidates_consumer_model(
    tmp_path, monkeypatch, write_fails
):
    """Delayed cancellation cannot leave the demand model selected against an old Energy membership."""
    from aiohttp import web

    from open_energy_planner.demand.runtime import DemandRuntime
    from open_energy_planner.home_assistant import setup_catalogue as setup_discovery

    async def scenario():
        """Call the actual registered authenticated-handler body with cancellation after its source write."""
        app = create_app(tmp_path, "cancel-catalogue-test-0123456789", source="home_assistant")
        runner = web.AppRunner(app)
        await runner.setup()
        changes = []
        select_learned = DemandRuntime.select_learned

        def select(runtime, enabled):
            """Observe and retain the real model-selection transition."""
            changes.append(enabled)
            return select_learned(runtime, enabled)

        async def cancel_after_write(write):
            """Model a successfully drained writer whose request was cancelled while waiting."""
            if write_fails:
                raise OSError("Simulated source persistence failure")
            write()
            return asyncio.CancelledError()

        class Request:
            """Supply valid already-authenticated catalogue JSON to isolate state installation ordering."""

            def clone(self, *, client_max_size):
                """Retain the handler's explicit bounded catalogue transport contract."""
                assert client_max_size == 1024 * 1024
                return self

            async def json(self):
                """Return ordinary HA discovery metadata, changing the formerly unknown membership."""
                return setup_metadata()

        monkeypatch.setattr(DemandRuntime, "select_learned", select)
        monkeypatch.setattr(setup_discovery, "complete_write", cancel_after_write)
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST" and route.resource.canonical == "/v1/ha/catalogue"
        )
        try:
            with pytest.raises(OSError if write_fails else asyncio.CancelledError):
                await handler(Request())
            assert False in changes
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_setup_catalogue_does_not_accept_unbounded_or_arbitrary_ha_attributes():
    """Only bounded sensor identity/unit metadata and typed Energy hints are accepted. Actual states and secret-bearing attributes are rejected."""
    catalogue = HACatalogue()
    assert catalogue.current(datetime.now(UTC))["stale"] is True
    body = setup_metadata()
    catalogue.receive(body, datetime.now(UTC))
    assert catalogue.current(datetime.now(UTC))["data"]["entities"][0]["name"] == "Grid"
    body["entities"][0]["state"] = "123"
    with pytest.raises(ValueError, match="Invalid setup entity"):
        catalogue.receive(body, datetime.now(UTC))
    body["entities"] = [{}] * 1025
    with pytest.raises(ValueError, match="Invalid setup entity"):
        catalogue.receive(body, datetime.now(UTC))


def test_setup_catalogue_rejects_false_readiness_and_unbounded_history():
    """A ready flag cannot replace a complete meter balance, and compact history cannot smuggle an unbounded Recorder copy."""
    body = setup_metadata()
    body["suggestions"]["terms"] = []
    with pytest.raises(ValueError, match="readiness"):
        HACatalogue().receive(body, datetime.now(UTC))
    body = setup_metadata()
    body["history"] = [
        {"entity_id": "sensor.import", "points": [[index * 86400, index] for index in range(3)]}
    ]
    with pytest.raises(ValueError, match="history points"):
        HACatalogue().receive(body, datetime.now(UTC))


@pytest.mark.parametrize(
    "association,expected", [(None, 400), ("sensor.old_battery", 400), ("sensor.new_battery", 200)]
)
def test_identity_free_battery_requires_current_energy_association(tmp_path, association, expected):
    """Replacing an Energy battery cannot reuse an old association. Only the current HA-owned links/capacity pass."""

    async def scenario():
        """Exercise preview with explicit Energy links, independently of measurement history."""
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer setup-test-token-0123456789"}
            catalogue = setup_metadata()
            catalogue["suggestions"]["batteries"] = [
                {
                    "energy_entity": "sensor.new_battery",
                    "charge_energy_entity": "sensor.new_charge",
                    "soc_entity": "sensor.soc",
                    "power_entity": "sensor.battery_power",
                    "capacity_kwh": 10,
                }
            ]
            catalogue["suggestions"]["terms"].append({"entity_id": "sensor.new_battery", "sign": 1})
            catalogue["suggestions"]["terms"].append({"entity_id": "sensor.new_charge", "sign": -1})
            for entity, kind, unit in [
                ("sensor.new_battery", "energy", "kWh"),
                ("sensor.new_charge", "energy", "kWh"),
                ("sensor.soc", "battery", "%"),
                ("sensor.battery_power", "power", "W"),
            ]:
                catalogue["entities"].append(
                    {
                        "entity_id": entity,
                        "name": entity,
                        "unit": unit,
                        "device_id": "battery",
                        "device_class": kind,
                        "state_class": "total_increasing" if kind == "energy" else "measurement",
                    }
                )
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=catalogue)
            ).status == 200
            battery = {
                "device_id": "battery",
                "inverter_id": "converter",
                "soc_entity": "sensor.soc",
                "power_entity": "sensor.battery_power",
                "charge_energy_entity": "sensor.new_charge",
                "capacity_kwh": 10,
                "charge_limit_w": 2000,
                "discharge_limit_w": 2000,
            }
            if association is not None:
                battery["energy_entity"] = association
            body = {
                "installation": {
                    "grid_power_entity": "sensor.grid",
                    "import_limit_w": 5000,
                    "export_limit_w": 5000,
                    "inverters": [
                        {
                            "inverter_id": "converter",
                            "ac_limit_w": 2000,
                            "efficiency": 0.96,
                            "hybrid": False,
                        }
                    ],
                    "batteries": [battery],
                },
                "solar": {"sources": [], "inverters": []},
                "demand": {
                    "annual_kwh": 3500,
                    "timezone": "Europe/Amsterdam",
                    "terms": catalogue["suggestions"]["terms"],
                    "exclusions": [],
                },
            }
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == expected, await response.text()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_save", [False, True])
def test_energy_membership_rejects_independent_writes_and_projects_pending_devices(
    tmp_path, monkeypatch, failed_save
):
    """Energy owns membership: removal revokes active state and retains history. A failed save preserves settings while keeping the plan revoked."""
    from copy import deepcopy
    from dataclasses import replace

    from open_energy_planner.demand.wire import parse_configuration as parse_demand
    from open_energy_planner.installation.storage import parse as parse_installation
    from open_energy_planner.live_inputs import LiveInputs
    from open_energy_planner.solar.storage import SolarStore
    from open_energy_planner.solar.wire import parse_configuration as parse_solar
    from open_energy_planner.synthetic import capture
    from tests.ha_contract.support.commands import command_plan

    invalidations = []
    live_instances = []
    original_invalidate = LiveInputs.invalidate

    def track_invalidation(live, reason):
        """Record the pre-reconciliation mismatch so automatic repair cannot hide plan revocation."""
        invalidations.append((reason, live.configuration_issue()))
        live_instances.append(live)
        original_invalidate(live, reason)

    monkeypatch.setattr(LiveInputs, "invalidate", track_invalidation)

    async def scenario():
        """Exercise authenticated write routes rather than relying on hidden browser controls."""
        headers = {"Authorization": "Bearer setup-test-token-0123456789"}
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            metadata = setup_metadata()
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            solar = {
                "sources": [
                    {
                        "source_id": "roof",
                        "entity_id": "sensor.roof",
                        "inverter_id": "roof",
                        "capacity_w": 3000,
                        "orientation": "south",
                    }
                ],
                "inverters": [{"inverter_id": "roof", "ac_limit_w": 3000}],
            }
            response = await client.put("/v1/solar", headers=headers, json=solar)
            assert response.status == 400
            assert not list_documents(tmp_path, "solar")
            from tests.app.installation.test_installation_app import installation

            response = await client.put("/v1/installation", headers=headers, json=installation(1))
            assert response.status == 400
            assert not list_documents(tmp_path, "installation")
            metadata["suggestions"].update(solar=True, solar_sources=["sensor.roof"])
            metadata["suggestions"]["solar_configurations"] = [
                {
                    "entity_id": "sensor.roof",
                    "providers": [],
                    "issue": "solar_configuration_not_available",
                }
            ]
            metadata["suggestions"]["terms"].append({"entity_id": "sensor.roof", "sign": 1})
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            state = await (await client.get("/v1/setup", headers=headers)).json()
            device = state["devices"][0]
            assert device["configured"] is False and device["energy_entity"] == "sensor.roof"
            assert (
                state["devices"]
                == (await (await client.get("/v1/setup", headers=headers)).json())["devices"]
            )
            assert (
                await client.put(
                    "/v1/solar", headers=headers, json={"sources": [], "inverters": []}
                )
            ).status == 400

            physical_body = installation()
            assert (
                await client.put("/v1/installation", headers=headers, json=physical_body)
            ).status == 200
            demand_body = {
                "terms": metadata["suggestions"]["terms"],
                "annual_kwh": 3500,
                "timezone": "Europe/Amsterdam",
            }
            assert (await client.put("/v1/demand", headers=headers, json=demand_body)).status == 200
            assert (await client.put("/v1/solar", headers=headers, json=solar)).status == 200
            before = await (await client.get("/v1/setup", headers=headers)).json()
            removed = setup_metadata()
            invalidations.clear()
            live = live_instances[-1]
            published = command_plan()
            live.repository.state["plan"] = published
            live.repository.save()
            active = capture(datetime.now(UTC))
            live.current_snapshot = active
            live.planner.latest = active
            assert live.current_snapshot is active and live.planner.latest is active

            def fail_save(_store, _configuration):
                """Simulate a settings-write failure after a published plan has become incompatible."""
                raise OSError("Test settings write failed")

            if failed_save:
                monkeypatch.setattr(SolarStore, "save", fail_save)
            response = await client.post("/v1/ha/catalogue", headers=headers, json=removed)
            assert response.status == (500 if failed_save else 200)
            assert invalidations[0] == (
                "energy_configuration_changed",
                "energy_configuration_changed",
            )
            assert live.repository.state["plan"] is None
            assert live.current_snapshot is None
            assert live.planner.latest is None

            assert live.repository.state["plan"] is None
            assert read_plan(tmp_path, published["plan_id"]) is not None
            if failed_save:
                assert live.planner.latest is None
                assert live.planner.reason == "energy_configuration_changed"
                assert (await (await client.get("/v1/setup", headers=headers)).json())[
                    "solar"
                ] == before["solar"]
                return
            reconciled = await (await client.get("/v1/setup", headers=headers)).json()
            assert reconciled["devices"] == []
            assert reconciled["solar"]["sources"] == []
            assert reconciled["demand"]["terms"] == removed["suggestions"]["terms"]
            assert "configuration-" + before["solar"]["version"] + ".json" in list_documents(
                tmp_path, "solar"
            )

            catalogue = HACatalogue()
            catalogue.receive(metadata, datetime.now(UTC))
            physical = parse_installation(installation())
            pv = parse_solar(solar)
            demand = parse_demand(
                {
                    "terms": metadata["suggestions"]["terms"],
                    "annual_kwh": 3500,
                    "timezone": "Europe/Amsterdam",
                }
            )
            assert catalogue.configuration_issue(physical, pv, demand, required=True) is None
            known = [{**device, "configured": True, "configuration": {"capacity_w": 3000}}]
            changed = deepcopy(metadata)
            changed["suggestions"].update(solar=False, solar_sources=[])
            changed["suggestions"]["solar_configurations"] = []
            changed["suggestions"]["terms"] = changed["suggestions"]["terms"][:1]
            catalogue.receive(changed, datetime.now(UTC))
            assert catalogue.devices(known) == []
            assert (
                catalogue.configuration_issue(physical, pv, demand, required=True)
                == "energy_configuration_changed"
            )
            assert pv.sources[0].source_id == "roof"
            assert (
                catalogue.configuration_issue(replace(physical, batteries=()), pv, demand)
                == "energy_configuration_changed"
            )

    asyncio.run(scenario())


def test_restart_requests_energy_configuration_without_opening_wizard(tmp_path):
    """A restarted HA-mode app requests fresh Energy metadata through snapshots so planning can resume without opening setup."""

    async def scenario():
        """Use only the integration snapshot/catalogue routes. Never request the wizard or settings endpoint."""
        token = "local-test-api-token-at-least-24-chars"
        headers = {"Authorization": "Bearer " + token}
        for _ in range(2):
            app = create_app(tmp_path, token, source="home_assistant")
            async with TestClient(TestServer(app)) as client:
                snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
                assert snapshot["installation"]["setup_discovery_requested"]
                response = await client.post(
                    "/v1/ha/catalogue", headers=headers, json=setup_metadata()
                )
                assert response.status == 200
                snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
                assert snapshot["installation"]["setup_discovery_requested"] is None

    asyncio.run(scenario())


def test_discovery_refresh_coalesces_pending_requests_and_renews_after_publication(tmp_path):
    """Explicit refresh requests coalesce while pending and renew their marker after each publication."""

    async def scenario():
        """Exercise the authenticated setup and catalogue API over two completed refresh cycles."""
        token = "local-test-api-token-at-least-24-chars"
        headers = {"Authorization": "Bearer " + token}
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            markers = []
            for _ in range(2):
                response = await client.get("/v1/setup?refresh=1", headers=headers)
                assert response.status == 200
                snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
                marker = snapshot["installation"]["setup_discovery_requested"]
                assert marker and marker not in markers
                markers.append(marker)
                response = await client.get("/v1/setup?refresh=1", headers=headers)
                assert response.status == 200
                snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
                assert snapshot["installation"]["setup_discovery_requested"] == marker
                response = await client.post(
                    "/v1/ha/catalogue", headers=headers, json=setup_metadata()
                )
                assert response.status == 200
                snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
                assert snapshot["installation"]["setup_discovery_requested"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "country, expected, choices",
    [
        ("NL", "NL", ["NL"]),
        ("DE", "DE-LU", ["DE-LU"]),
        ("NO", None, ["NO1", "NO2", "NO3", "NO4", "NO5"]),
        ("DK", None, ["DK1", "DK2"]),
        ("SE", None, ["SE1", "SE2", "SE3", "SE4"]),
        ("IT", None, ["IT-NORD", "IT-CNOR", "IT-CSUD", "IT-SUD", "IT-CALA", "IT-SICI", "IT-SARD"]),
        (None, None, []),
    ],
)
def test_market_area_hint_uses_only_unambiguous_ha_country(tmp_path, country, expected, choices):
    """An HA country prefills a unique zone. Denmark/Norway and absent metadata never guess one."""

    async def scenario():
        """Use the authenticated discovery and setup endpoints. Timezone alone cannot choose a zone."""
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer setup-test-token-0123456789"}
            body = setup_metadata()
            body["country"] = country
            response = await client.post("/v1/ha/catalogue", headers=headers, json=body)
            assert response.status == 200
            response = await client.get("/v1/setup", headers=headers)
            settings = await response.json()
            assert settings["market_area_hint"] == expected
            assert settings["market_area_options"] == choices
            for invalid in ("Netherlands", [], 123, "N1"):
                body["country"] = invalid
                response = await client.post("/v1/ha/catalogue", headers=headers, json=body)
                assert response.status == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_individual_consumer_history_response_routes_and_stale_rejection(tmp_path, failed):
    """Consumer history uses its own cohort, reports errors honestly, and rejects a replayed response."""
    from copy import deepcopy

    async def scenario():
        """Complete the primary request first, then exercise the actual supplemental history endpoint."""
        headers = {"Authorization": "Bearer consumer-history-0123456789abcdef"}
        async with TestClient(
            TestServer(
                create_app(tmp_path, "consumer-history-0123456789abcdef", source="home_assistant")
            )
        ) as client:
            metadata = deepcopy(setup_metadata())
            metadata["suggestions"]["consumers"] = [{"entity_id": "sensor.heat", "upstream": None}]
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            configuration = {
                "terms": metadata["suggestions"]["terms"],
                "annual_kwh": 3500,
                "timezone": "Europe/Amsterdam",
                "exclusions": [],
                "expected_version": None,
            }
            response = await client.put("/v1/demand", headers=headers, json=configuration)
            assert response.status == 200
            primary = (await response.json())["request"]
            from tests.app.demand.history_support import history_report
            from tests.app.demand.test_demand import channel

            while primary is not None:
                primary_body = history_report(primary, channel("sensor.import", [0.4] * 48))
                assert (
                    await client.post("/v1/demand/history", headers=headers, json=primary_body)
                ).status == 200
                primary = (await (await client.get("/v1/demand", headers=headers)).json())[
                    "request"
                ]
            state = await (await client.get("/v1/consumers", headers=headers)).json()
            assert (
                await client.put(
                    "/v1/consumers",
                    headers=headers,
                    json={
                        "expected_version": state["version"],
                        "selected": [{"entity_id": "sensor.heat", "weather_sensitive": False}],
                    },
                )
            ).status == 200
            snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
            pending = snapshot["demand"]["request"]
            assert pending["entity_id"] == "sensor.heat"
            body = history_report(pending, channel("sensor.heat", [0.2] * 48))
            if failed:
                body = {"request_version": pending["version"], "error": "history_unavailable"}
            response = await client.post("/v1/demand/history", headers=headers, json=body)
            assert response.status == 200
            state = await (await client.get("/v1/consumers", headers=headers)).json()
            assert state["history_issues"] == (
                {"sensor.heat": "history_unavailable"} if failed else {}
            )
            assert (
                await client.post("/v1/demand/history", headers=headers, json=body)
            ).status == 409

    asyncio.run(scenario())


def test_contract_dates_and_bonus_clocks_follow_authoritative_ha_timezone(tmp_path):
    """HA-local dates ignore a client-supplied bonus zone. A HA clock change creates a revision while old timestamps stay frozen."""

    async def scenario():
        """Use authenticated catalogue/contract endpoints and reread the persisted active contract after HA changes."""
        headers = {"Authorization": "Bearer setup-test-token-0123456789"}
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            metadata = setup_metadata()
            metadata["timezone"] = "Europe/Amsterdam"
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            session = await (await client.get("/setup/session")).json()
            assert session["timezone"] == "Europe/Amsterdam"
            body = {
                "contract_id": "calendar",
                "market_area": "NL",
                "currency": "EUR",
                "expected_version": None,
                "periods": [
                    {
                        "effective_from": "2026-01-01",
                        "effective_until": None,
                        "import_formula": "market",
                        "export_formula": "market",
                    }
                ],
                "bonuses": [
                    {
                        "rule_id": "bonus",
                        "effective_from": "2026-07-01",
                        "rate_formula": "0.01",
                        "cap_kwh": 1000,
                        "timezone": "America/New_York",
                    }
                ],
            }
            response = await client.put("/v1/contracts", headers=headers, json=body)
            assert response.status == 200, await response.text()
            before = await response.json()
            assert before["periods"][0]["effective_from"] == "2025-12-31T23:00:00+00:00"
            assert before["bonuses"][0]["effective_from"] == "2026-06-30T22:00:00+00:00"
            assert before["bonuses"][0]["timezone"] == "Europe/Amsterdam"
            metadata["timezone"] = "Europe/London"
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            after = await (await client.get("/v1/contracts", headers=headers)).json()
            assert after["version"] != before["version"]
            assert after["bonuses"][0]["timezone"] == "Europe/London"
            assert after["periods"] == before["periods"]

    asyncio.run(scenario())
