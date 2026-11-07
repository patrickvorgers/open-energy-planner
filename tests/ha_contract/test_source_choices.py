"""Keep temporary chooser captures complete, expiring and isolated from active source membership."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.home_assistant.catalogue import HACatalogue
from open_energy_planner.home_assistant.source_choices import configured_sources
from tests.ha_contract.support.metadata import setup_metadata


def test_candidate_acceptance_never_changes_active_sources_and_expires():
    """A chooser can expose an unconfigured car without replacing Energy or extending its freshness."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    owner = HACatalogue()
    active = setup_metadata()
    owner.receive(active, now)
    candidates = deepcopy(active)
    candidates["entities"].append(
        {
            "entity_id": "sensor.zeekr_soc",
            "name": "Zeekr battery",
            "unit": "%",
            "device_id": "car",
            "device_class": "battery",
            "state_class": "measurement",
        }
    )
    nonce = owner.choices.request("ev", now)
    assert owner.choices.request("ev", now) == nonce
    assert owner.choices.status("ev", active, now)["catalogue"] is None
    assert owner.choices.receive({"scope": "ev", "id": nonce, "page": candidates}, now)
    assert owner.value is active
    assert owner.confirmed == now
    assert len(owner.choices.catalogue("ev", active, now)["entities"]) == 3
    assert len(owner.choices.catalogue("thermal", active, now)["entities"]) == 2
    assert owner.choices.catalogue("ev", active, now + timedelta(minutes=5)) is active
    assert owner.choices.results == {}


def test_expired_failed_and_superseded_requests_cannot_publish():
    """Only a matching current request can replace an editor's complete accepted result."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    owner = HACatalogue()
    first = owner.choices.request("ev", now)
    assert not owner.choices.receive({"scope": "ev", "id": first, "failed": True}, now)
    assert owner.choices.status("ev", None, now)["failed"]
    second = owner.choices.request("ev", now)
    assert first != second
    with pytest.raises(ValueError):
        owner.choices.receive({"scope": "ev", "id": first, "page": setup_metadata()}, now)
    later = now + timedelta(seconds=60)
    assert owner.choices.pending(later) == []
    with pytest.raises(ValueError):
        owner.choices.receive({"scope": "ev", "id": second, "page": setup_metadata()}, later)
    assert owner.choices.status("ev", None, later)["failed"]


@pytest.mark.parametrize("scope", ["ev", "thermal"])
@pytest.mark.parametrize("unit", ["", "incompatible"])
def test_current_active_metadata_wins_over_older_chooser_records(scope, unit):
    """An accepted temporary choice must never resurrect an unavailable or incompatible active source."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    owner = HACatalogue()
    older = setup_metadata()
    candidates = deepcopy(older)
    candidates["entities"].append({**older["entities"][0], "entity_id": "sensor.new_choice"})
    nonce = owner.choices.request(scope, now)
    owner.choices.receive({"scope": scope, "id": nonce, "page": candidates}, now)
    current = deepcopy(older)
    current["entities"][0].update(unit=unit, name="Current source metadata")
    owner.receive(current, now + timedelta(seconds=1))
    merged = owner.choices.catalogue(scope, owner.value, now + timedelta(seconds=2))
    records = {row["entity_id"]: row for row in merged["entities"]}
    assert records["sensor.grid"] == current["entities"][0]
    assert records["sensor.new_choice"] == candidates["entities"][-1]
    assert owner.value is current


@pytest.mark.parametrize("scope", ["ev", "thermal"])
@pytest.mark.parametrize("unit", ["", "incompatible"])
def test_stale_chooser_cannot_authorize_a_write_with_incompatible_active_sources(
    tmp_path, scope, unit
):
    """Real configuration writes reject stale candidate units and preserve the confirmed configuration."""
    from aiohttp import ClientSession

    from open_energy_planner.app import create_app
    from tests.ha_contract.support.connection import server

    async def scenario():
        """Publish an old chooser, change active metadata and try a version-correct configuration write."""
        token = "source-choice-admin-synthetic-0123456789"
        headers = {"Authorization": "Bearer " + token}
        active = setup_metadata()
        source = "sensor.synthetic_source"
        for identity, quantity, source_unit in (
            (source, "battery" if scope == "ev" else "temperature", "%" if scope == "ev" else "°C"),
            ("sensor.boiler_energy", "energy", "kWh"),
            ("sensor.boiler_power", "power", "W"),
        ):
            active["entities"].append(
                {
                    "entity_id": identity,
                    "name": "Synthetic source",
                    "device_id": "synthetic",
                    "unit": source_unit,
                    "device_class": quantity,
                    "state_class": "total_increasing" if quantity == "energy" else "measurement",
                }
            )
        active["suggestions"]["consumers"] = [
            {"entity_id": "sensor.boiler_energy", "upstream": None}
        ]
        identity = "00000000-0000-4000-8000-000000000101"
        configuration = (
            {
                "vehicles": [
                    {
                        "id": identity,
                        "soc_entity": source,
                        "capacity_kwh": 60,
                        "maximum_w": 11000,
                        "target_soc": 80,
                        "departure_time": "07:00",
                    }
                ],
                "points": [],
            }
            if scope == "ev"
            else {
                "stores": [
                    {
                        "id": identity,
                        "energy_entity": "sensor.boiler_energy",
                        "temperature_entity": source,
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
        )
        async with (
            server(create_app(tmp_path, token, source="home_assistant")) as url,
            ClientSession(headers=headers) as client,
        ):
            async with client.post(url + "/v1/ha/catalogue", json=active) as response:
                assert response.status == 200
            async with client.post(url + "/v1/ha/source-choices/" + scope, json={}) as response:
                nonce = (await response.json())["id"]
            async with client.post(
                url + "/v1/ha/source-choices",
                json={"scope": scope, "id": nonce, "page": active},
            ) as response:
                assert response.status == 200
            async with client.get(url + "/v1/" + scope) as response:
                version = (await response.json())["configuration_version"]
            async with client.post(
                url + "/v1/" + scope + "/configuration",
                json={"configuration": configuration, "expected_version": version},
            ) as response:
                assert response.status == 200
            changed = deepcopy(active)
            changed["entities"][2]["unit"] = unit
            async with client.post(url + "/v1/ha/catalogue", json=changed) as response:
                assert response.status == 200
            async with client.get(url + "/v1/" + scope) as response:
                version = (await response.json())["configuration_version"]
            async with client.post(
                url + "/v1/" + scope + "/configuration",
                json={"configuration": configuration, "expected_version": version},
            ) as response:
                assert response.status == 400
            async with client.get(url + "/v1/" + scope) as response:
                assert (await response.json())["configuration_version"] == version

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["all", "config_entries", "../ev", ""])
def test_arbitrary_ha_queries_are_not_supported(scope):
    """Finite editor scopes cannot be widened into arbitrary provider or registry access."""
    with pytest.raises(ValueError):
        HACatalogue().choices.request(scope, datetime.now(UTC))


def test_configured_extra_bindings_are_explicit_and_include_disabled_appliances():
    """Preserve registered EV and boiler sources independently from Energy and enabled planning."""
    from types import SimpleNamespace

    assert configured_sources(
        SimpleNamespace(bindings=(SimpleNamespace(entity_id="sensor.grid"),)),
        {
            "vehicles": [{"soc_entity": "sensor.ev", "label": "sensor.not_a_binding"}],
            "points": [
                {
                    "energy_entity": "sensor.energy",
                    "power_entity": "sensor.power",
                    "connected_entity": "binary_sensor.connected",
                }
            ],
        },
        {
            "stores": [
                {
                    "enabled": False,
                    "energy_entity": "sensor.energy",
                    "temperature_entity": "water_heater.boiler",
                    "power_entity": "sensor.heater_power",
                }
            ]
        },
    ) == [
        "binary_sensor.connected",
        "sensor.energy",
        "sensor.ev",
        "sensor.grid",
        "sensor.heater_power",
        "sensor.power",
        "water_heater.boiler",
    ]


@pytest.mark.parametrize("corrupt", [False, True])
def test_partial_or_corrupt_chooser_pages_retain_previous_complete_choices(corrupt):
    """Fragment receipt cannot expose a partial list or erase the previous complete choices."""
    from tests.ha_contract.test_catalogue_transfer import snapshot_pages

    now = datetime(2026, 11, 7, tzinfo=UTC)
    owner = HACatalogue()
    active = setup_metadata()
    owner.receive(active, now)
    choices = owner.choices
    first = choices.request("ev", now)
    choices.receive({"scope": "ev", "id": first, "page": active}, now)
    second = choices.request("ev", now)
    replacement = deepcopy(active)
    replacement["entities"][0]["name"] = "New candidate name"
    replacement["entities"][0]["entity_id"] = "sensor.new_candidate"
    pages = snapshot_pages(replacement, now)
    for page in pages[:-1]:
        assert not choices.receive({"scope": "ev", "id": second, "page": page}, now)
        assert choices.catalogue("ev", active, now)["entities"][0]["name"] == "Grid"
    final = pages[-1]
    if corrupt:
        final["transfer"]["revision"] = "0" * 64
        with pytest.raises(ValueError):
            choices.receive({"scope": "ev", "id": second, "page": final}, now)
        assert choices.catalogue("ev", active, now)["entities"][0]["name"] == "Grid"
    else:
        assert choices.receive({"scope": "ev", "id": second, "page": final}, now)
        records = {
            row["entity_id"]: row for row in choices.catalogue("ev", active, now)["entities"]
        }
        assert records["sensor.new_candidate"]["name"] == "New candidate name"
        assert records["sensor.grid"]["name"] == "Grid"
    assert owner.value is active
