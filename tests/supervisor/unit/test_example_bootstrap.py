"""Verify safe recovery from lost HA responses and revalidation of current product bindings."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from script.home_assistant.bootstrap import provision_ha
from script.home_assistant.bootstrap import provision_ha_examples as ha_examples
from script.provisioning import device_examples as examples
from tests.supervisor.support.examples import HomeAssistantBoundary, run


@pytest.mark.parametrize(
    "change", [None, "settings", "energy_and_settings", "missing_receipt", "removed_energy"]
)
def test_retained_power_automation_ownership_requires_original_write(tmp_path, monkeypatch, change):
    """Retained Energy power is valid, but a later edit or absent origin never authorizes new controls."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    custom_power = "sensor.retained_wallbox_power"
    boundary.ev["points"][0]["power_entity"] = custom_power
    boundary.options[0]["power_entity"] = custom_power
    journal.state["ev_configuration"]["points"][0]["power_entity"] = custom_power
    if change == "missing_receipt":
        del journal.state["ev_configuration"]
    elif change == "removed_energy":
        boundary.options.pop(0)
    elif change:
        boundary.ev["points"][0]["power_entity"] = "sensor.user_wallbox_power"
        if change == "energy_and_settings":
            boundary.options[0]["power_entity"] = "sensor.user_wallbox_power"
    journal.save()
    before = deepcopy((boundary.ev, boundary.options))
    run(boundary, journal, monkeypatch)
    assert (boundary.ev, boundary.options) == before
    assert ("owns:ev" in examples.ExampleJournal(journal.path).state["completed"]) == (
        change is None
    )
    assert ("oep_lab_wallbox_a" in boundary.definitions) == (change is None)
    assert ("oep_lab_wallbox_b" in boundary.definitions) == (change is None)
    boundary.calls.clear()
    run(boundary, journal, monkeypatch)
    assert boundary.calls == []


def test_example_mode_requires_current_product_api_before_ha_changes(tmp_path, monkeypatch):
    """An incomplete helper invocation is rejected before account files or HA requests can be created."""
    monkeypatch.delenv("OEP_DEV_HA_URL", raising=False)
    with pytest.raises(ValueError, match="current product API session"):
        asyncio.run(provision_ha.provision(examples_journal=tmp_path / "examples.json"))
    assert not (tmp_path / "examples.json").exists()


@pytest.mark.parametrize(
    "operation",
    [
        "/addons/local_open_energy_planner/options",
        "/api/config/automation/config/oep_lab_dishwasher",
    ],
)
def test_lost_successful_ha_response_does_not_repeat_creation(tmp_path, monkeypatch, operation):
    """A partially completed startup resumes after observing the object already saved by HA."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    boundary.fail_after = operation
    with pytest.raises(ConnectionError):
        run(boundary, journal, monkeypatch)
    run(boundary, journal, monkeypatch)
    assert len(boundary.definitions) == 6 and boundary.sidebar
    assert sum(method == "POST" and path == operation for method, path, _ in boundary.calls) == 1


@pytest.mark.parametrize("removed", ["sidebar", "automation"])
def test_hidden_or_removed_ha_example_after_ambiguous_write_is_retained(
    tmp_path, monkeypatch, removed
):
    """Hiding the panel or deleting an automation after a lost response does not invite a retry overwrite."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    operation = (
        "/addons/local_open_energy_planner/options"
        if removed == "sidebar"
        else "/api/config/automation/config/oep_lab_dishwasher"
    )
    boundary.fail_after = operation
    with pytest.raises(ConnectionError):
        run(boundary, journal, monkeypatch)
    if removed == "sidebar":
        boundary.sidebar = False
    else:
        del boundary.definitions["oep_lab_dishwasher"]
    boundary.calls.clear()
    run(boundary, journal, monkeypatch)
    assert not any(method == "POST" and path == operation for method, path, _ in boundary.calls)
    if removed == "sidebar":
        assert not boundary.sidebar
    else:
        assert "oep_lab_dishwasher" not in boundary.definitions


def test_explicitly_rejected_automation_can_retry_after_correction(tmp_path, monkeypatch):
    """An HA validation rejection is not an ambiguous write and must not suppress a corrected retry."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    original_request = boundary.request
    rejected = False

    async def request(method, path, **kwargs):
        """Reject the first dishwasher configuration without creating its native automation."""
        nonlocal rejected
        if not rejected and method == "POST" and path.endswith("/oep_lab_dishwasher"):
            rejected = True
            raise ClientResponseError(
                SimpleNamespace(real_url="http://homeassistant" + path), (), status=400
            )
        return await original_request(method, path, **kwargs)

    boundary.request = request
    with pytest.raises(ClientResponseError):
        run(boundary, journal, monkeypatch)
    assert (
        "attempted:automation:dishwasher"
        not in examples.ExampleJournal(journal.path).state["completed"]
    )
    run(boundary, journal, monkeypatch)
    assert len(boundary.definitions) == 6


@pytest.mark.parametrize(
    "family,field,value",
    [
        ("ev", "maximum_w", 12000),
        ("ev", "energy_entity", "sensor.user_wallbox_energy"),
        ("thermal", "heater_w", 1000),
        ("thermal", "temperature_entity", "sensor.user_boiler_temperature"),
    ],
)
def test_pending_automations_revalidate_confirmed_but_edited_bindings(
    tmp_path, monkeypatch, family, field, value
):
    """A confirmed historical receipt cannot authorize fixed simulator actions after current product edits."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    settings = boundary.ev["points"] if family == "ev" else boundary.thermal["stores"]
    settings[0][field] = value
    before = deepcopy(settings)
    run(boundary, journal, monkeypatch)
    assert settings == before
    completed = examples.ExampleJournal(journal.path).state["completed"]
    assert "owns:" + family not in completed
    forbidden = {"oep_lab_wallbox_a", "oep_lab_wallbox_b"} if family == "ev" else {"oep_lab_boiler"}
    assert not forbidden & boundary.definitions.keys()
    assert len(boundary.definitions) == (4 if family == "ev" else 5)


@pytest.mark.parametrize("edit", ["deselected", "deleted", "unshiftable"])
def test_pending_appliance_actions_respect_current_product_state(tmp_path, monkeypatch, edit):
    """Retained task-status entities do not authorize actions after user changes or removal."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    if edit == "deselected":
        boundary.consumers.pop(0)
    elif edit == "deleted":
        boundary.appliances.pop(0)
    else:
        boundary.appliances[0]["shiftable"] = False
    before = deepcopy((boundary.consumers, boundary.appliances))
    run(boundary, journal, monkeypatch)
    assert "oep_lab_dishwasher" not in boundary.definitions
    assert len(boundary.definitions) == 5
    assert (boundary.consumers, boundary.appliances) == before
    boundary.calls.clear()
    run(boundary, journal, monkeypatch)
    assert not boundary.calls


def test_pending_bindings_are_checked_again_during_registry_wait(tmp_path, monkeypatch):
    """A limit edit during delayed discovery prevents wallbox creation on the following poll."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    boundary.registry = [
        row
        for row in boundary.registry
        if row["entity_id"] != "binary_sensor.renamed_wallbox_a_charging"
    ]
    delays = []

    async def sleep(seconds):
        """Apply the user's valid product limit while the virtual registry is still incomplete."""
        delays.append(seconds)
        boundary.ev["points"][0]["maximum_w"] = 12000

    monkeypatch.setattr(ha_examples.asyncio, "sleep", sleep)
    run(boundary, journal, monkeypatch)
    assert delays == [2]
    assert "oep_lab_wallbox_a" not in boundary.definitions and len(boundary.definitions) == 4
    assert boundary.ev["points"][0]["maximum_w"] == 12000


def test_unavailable_product_api_does_not_authorize_pending_actions(tmp_path, monkeypatch):
    """A failed fresh product read aborts before automation writes, without trusting an old owns receipt."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    original = boundary.app_request

    async def unavailable(_method, _path):
        """Keep application-read failure distinct from virtual entity discovery delay."""
        raise ConnectionError("Product configuration unavailable")

    boundary.app_request = unavailable
    with pytest.raises(ConnectionError):
        run(boundary, journal, monkeypatch)
    assert not boundary.definitions
    boundary.app_request = original
    run(boundary, journal, monkeypatch)
    assert len(boundary.definitions) == 6
