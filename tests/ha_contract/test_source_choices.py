"""Keep temporary chooser captures complete, expiring and isolated from active source membership."""

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
        assert choices.catalogue("ev", active, now)["entities"][0]["name"] == "New candidate name"
    assert owner.value is active
