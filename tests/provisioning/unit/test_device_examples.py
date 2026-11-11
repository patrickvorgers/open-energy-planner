"""Exercise first, repeated and interrupted example provisioning without rewriting user evidence."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from open_energy_planner.appliances.cycles.learning import detect_cycles, manual_profile
from open_energy_planner.appliances.cycles.model import learn_cycle
from open_energy_planner.appliances.ev.configuration import configuration
from open_energy_planner.appliances.thermal.service import ThermalService
from open_energy_planner.contracts.wire import contract_document, parse_contract
from script.provisioning import device_examples as examples

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


class ProductBoundary:
    """Expose detached responses and normal validators, retaining every observed mutation."""

    def __init__(self):
        """Begin with the former foundation-only lab and independently named simulator sources."""
        self.calls = []
        self.fail_after = None
        self.selected = []
        self.devices = {}
        self.ev = {"vehicles": [], "points": []}
        self.thermal = {"stores": []}
        names = (
            "heat_pump",
            "dishwasher",
            "washing_machine",
            "dryer",
            "wallbox_a",
            "wallbox_b",
            "boiler",
        )
        self.options = [
            {
                "entity_id": f"sensor.oep_test_{name}_energy",
                "upstream": None,
                **(
                    {"power_entity": f"sensor.oep_test_{name}_power"}
                    if name in ("dishwasher", "washing_machine", "dryer", "wallbox_a", "wallbox_b")
                    else {}
                ),
            }
            for name in names
        ]
        self.setup = {
            "installation": {"saved": True},
            "solar": {"saved": True},
            "demand": {"saved": True},
            "catalogue": {"data": {"timezone": "Europe/Amsterdam"}},
        }
        self.contract = {"status": "not_configured", "version": None}
        self.catalogue = {"entities": [], "suggestions": {"consumers": self.options}}
        for suffix in ("a", "b"):
            self.catalogue["entities"].extend(
                [
                    {
                        "entity_id": f"sensor.oep_test_car_{suffix}_soc",
                        "unit": "%",
                        "device_class": "battery",
                    },
                    {
                        "entity_id": f"sensor.oep_test_wallbox_{suffix}_power",
                        "unit": "W",
                        "device_class": "power",
                    },
                    {
                        "entity_id": f"binary_sensor.oep_test_wallbox_{suffix}_connected",
                        "unit": "",
                        "device_class": "plug",
                    },
                ]
            )
        self.catalogue["entities"].extend(
            {"entity_id": entity}
            for entity in (
                "sensor.oep_test_wallbox_a_energy",
                "sensor.oep_test_wallbox_b_energy",
                "sensor.oep_test_boiler_energy",
                "sensor.oep_test_boiler_power",
                "sensor.oep_test_boiler_temperature",
            )
        )
        self.candidates = deepcopy(self.catalogue)
        self.catalogue["entities"] = [
            entity
            for entity in self.catalogue["entities"]
            if entity["entity_id"].endswith("_energy")
        ]
        self.setup["catalogue"]["data"]["entities"] = self.catalogue["entities"]
        self.choice_results = {}

    async def request(self, method, path, body=None):
        """Run domain validation at writes and optionally lose the successful HTTP response."""
        self.calls.append((method, path, deepcopy(body)))
        result = None
        if path.startswith("/v1/ha/source-choices/"):
            scope = path.rsplit("/", 1)[1]
            assert scope in ("ev", "thermal")
            if method == "POST":
                assert body == {}
                self.choice_results[scope] = {
                    **deepcopy(self.catalogue),
                    "entities": [
                        deepcopy(entity)
                        for entity in self.candidates["entities"]
                        if ("boiler" in entity["entity_id"]) == (scope == "thermal")
                    ],
                }
                return {"pending": True, "failed": False, "catalogue": None}
            assert method == "GET"
            return {
                "pending": False,
                "failed": False,
                "catalogue": deepcopy(self.choice_results.get(scope, self.catalogue)),
            }
        if method == "GET":
            result = {
                "/v1/setup": self.setup,
                "/v1/setup?refresh=1": self.setup,
                "/v1/consumers": {
                    "selected": self.selected,
                    "version": "consumer-version",
                    "options": self.options,
                },
                "/v1/appliances": {
                    "devices": list(self.devices.values()),
                    "revision": "appliance-version",
                },
                "/v1/ev": {**self.ev, "configuration_version": "ev-version"},
                "/v1/thermal": {
                    "configuration": self.thermal,
                    "configuration_version": "thermal-version",
                },
                "/v1/contracts": self.contract,
            }[path]
        elif path == "/v1/consumers":
            assert body["expected_version"] == "consumer-version"
            self.selected = deepcopy(body["selected"])
            result = {
                "selected": self.selected,
                "version": "consumer-version",
                "options": self.options,
            }
        elif path == "/v1/appliances/device":
            assert body["entity_id"] in {row["entity_id"] for row in self.selected}
            self.devices[body["entity_id"]] = {**body, "task": None}
        elif path == "/v1/appliances/history":
            cycles = detect_cycles(body["rows"], NOW)
            assert len(cycles) == (1 if "dryer" in body["entity_id"] else 2)
            start, end, points = cycles[0]
            learned = learn_cycle(points, start, end, synthetic=body["synthetic"])
            row = self.devices[body["entity_id"]]
            if len(cycles) == 1:
                return {"accepted": True}
            row["profile"] = {
                "power_w": learned.power_w,
                "samples": 2,
                "evidence": learned.evidence,
            }
        elif path == "/v1/appliances/manual_profile":
            assert body["expected_version"] == "appliance-version"
            estimate = manual_profile(body["duration_minutes"], body["energy_kwh"])
            self.devices[body["entity_id"]]["profile"] = {
                "power_w": estimate.power_w,
                "samples": 1,
                "evidence": "manual",
            }
        elif path == "/v1/ev/configuration":
            assert body["expected_version"] == "ev-version"
            self.ev, selected = configuration(
                body["configuration"],
                self.choice_results.get("ev", self.catalogue),
                SimpleNamespace(selected=self.selected, options=self.options),
                SimpleNamespace(state={"devices": self.devices}),
            )
            self.selected = list(selected)
        elif path == "/v1/thermal/configuration":
            assert body["expected_version"] == "thermal-version"
            ThermalService._shape(body["configuration"])
            self.thermal = deepcopy(body["configuration"])
        elif path == "/v1/contracts":
            assert body["expected_version"] == self.contract["version"]
            self.contract = contract_document(
                parse_contract(body, NOW, installation_timezone="Europe/Amsterdam")
            )
        else:
            pytest.fail("Unexpected product operation " + method + " " + path)
        if method != "GET" and self.fail_after == path:
            self.fail_after = None
            raise ConnectionError("Successful write response lost")
        return deepcopy(result or {})


def provision(boundary, path):
    """Restart the actual journal on every invocation, including interruption recovery."""
    asyncio.run(examples.configure(boundary.request, examples.ExampleJournal(path), NOW))


@pytest.mark.parametrize("lost_response", [False, True])
def test_existing_energy_power_binding_is_used_and_recovered(tmp_path, lost_response):
    """A valid retained Energy power source governs choices, the write and recovery ownership."""
    boundary = ProductBoundary()
    custom_power = "sensor.retained_wallbox_power"
    boundary.options[4]["power_entity"] = custom_power
    boundary.candidates["entities"].append(
        {"entity_id": custom_power, "unit": "W", "device_class": "power"}
    )
    boundary.candidates["entities"] = [
        row
        for row in boundary.candidates["entities"]
        if row["entity_id"] != "sensor.oep_test_wallbox_a_power"
    ]
    before = deepcopy(boundary.options)
    path = tmp_path / "examples.json"
    if lost_response:
        boundary.fail_after = "/v1/ev/configuration"
        with pytest.raises(ConnectionError):
            provision(boundary, path)
    provision(boundary, path)
    assert boundary.ev["points"][0]["power_entity"] == custom_power
    assert boundary.options == before
    assert "owns:ev" in examples.ExampleJournal(path).state["completed"]
    assert (
        sum(
            method == "POST" and route == "/v1/ev/configuration"
            for method, route, _ in boundary.calls
        )
        == 1
    )
    boundary.calls.clear()
    provision(boundary, path)
    assert boundary.calls == []


@pytest.mark.parametrize("change", ["settings", "energy_and_settings", "missing_receipt"])
def test_changed_power_after_ambiguous_write_is_not_claimed(tmp_path, change):
    """Recovery needs the original intended write, not just a match against today's Energy choices."""
    boundary = ProductBoundary()
    path = tmp_path / "examples.json"
    boundary.fail_after = "/v1/ev/configuration"
    with pytest.raises(ConnectionError):
        provision(boundary, path)
    if change == "missing_receipt":
        journal = examples.ExampleJournal(path)
        del journal.state["ev_configuration"]
        journal.save()
    else:
        custom_power = "sensor.user_wallbox_power"
        boundary.ev["points"][0]["power_entity"] = custom_power
        boundary.candidates["entities"].append(
            {"entity_id": custom_power, "unit": "W", "device_class": "power"}
        )
        if change == "energy_and_settings":
            boundary.options[4]["power_entity"] = custom_power
    before = deepcopy((boundary.ev, boundary.options))
    provision(boundary, path)
    assert (boundary.ev, boundary.options) == before
    assert "owns:ev" not in examples.ExampleJournal(path).state["completed"]
    assert (
        sum(
            method == "POST" and route == "/v1/ev/configuration"
            for method, route, _ in boundary.calls
        )
        == 1
    )


def test_energy_only_active_catalogue_uses_scoped_device_choices(tmp_path, monkeypatch):
    """Unconfigured cars and boiler sources are obtained through choices without enlarging active metadata."""
    boundary = ProductBoundary()
    active = deepcopy(boundary.setup["catalogue"])
    monkeypatch.setattr(examples, "FIXTURE_READY_ATTEMPTS", 1)
    provision(boundary, tmp_path / "examples.json")
    assert boundary.setup["catalogue"] == active
    assert [
        path
        for method, path, _ in boundary.calls
        if method == "POST" and "/source-choices/" in path
    ] == [
        "/v1/ha/source-choices/ev",
        "/v1/ha/source-choices/thermal",
    ]
    assert len(boundary.ev["vehicles"]) == 2 and len(boundary.thermal["stores"]) == 1


def test_first_start_configures_all_device_types_without_submitting_tasks(tmp_path):
    """Three independently specified profiles use .83/.92/1.4 kWh and leave readiness to the user."""
    boundary = ProductBoundary()
    provision(boundary, tmp_path / "examples.json")
    assert len(boundary.devices) == 3
    for name, energy, duration in (
        ("dishwasher", 0.83, 7),
        ("washing_machine", 0.92, 9),
        ("dryer", 1.4, 12),
    ):
        row = boundary.devices[f"sensor.oep_test_{name}_energy"]
        assert row["shiftable"] and row["task"] is None
        assert len(row["profile"]["power_w"]) == duration
        assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(energy)
        assert row["profile"]["samples"] == (1 if name == "dryer" else 2)
        assert row["profile"]["evidence"] == ("manual" if name == "dryer" else "synthetic")
    assert next(row for row in boundary.selected if "heat_pump" in row["entity_id"])[
        "weather_sensitive"
    ]
    assert [row["capacity_kwh"] for row in boundary.ev["vehicles"]] == [60, 40]
    assert [row["maximum_w"] for row in boundary.ev["points"]] == [11000, 7400]
    assert len(boundary.ev["vehicles"]) == len(boundary.ev["points"]) == 2
    assert all(len(row["vehicle_ids"]) == 2 for row in boundary.ev["points"])
    store = boundary.thermal["stores"][0]
    assert (store["volume_litres"], store["heater_w"], store["minimum_c"], store["maximum_c"]) == (
        200,
        2000,
        45,
        65,
    )
    assert sum(store["draw_profile_kwh"]) == pytest.approx(3.2)
    assert all(
        path not in ("/v1/appliances/tasks", "/v1/ev/assign", "/v1/thermal/observations")
        for _, path, _ in boundary.calls
    )


def test_warm_start_preserves_edits_deletions_profiles_and_tasks(tmp_path):
    """Completed receipts prevent deleted examples or manually changed readiness from returning."""
    boundary, path = ProductBoundary(), tmp_path / "examples.json"
    provision(boundary, path)
    boundary.devices["sensor.oep_test_dishwasher_energy"]["task"] = {
        "status": "running",
        "user_request": "retain",
    }
    boundary.devices.pop("sensor.oep_test_dryer_energy")
    boundary.selected = []
    boundary.ev = {"vehicles": [], "points": []}
    boundary.thermal["stores"][0]["enabled"] = False
    before = deepcopy((boundary.devices, boundary.selected, boundary.ev, boundary.thermal))
    boundary.calls.clear()
    provision(boundary, path)
    assert before == (boundary.devices, boundary.selected, boundary.ev, boundary.thermal)
    assert boundary.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        "/v1/consumers",
        "/v1/appliances/device",
        "/v1/appliances/history",
        "/v1/ev/configuration",
        "/v1/thermal/configuration",
    ],
)
def test_interrupted_start_resumes_without_duplicate_profiles_or_identities(tmp_path, operation):
    """Losing a successful response must not repeat a completed cycle or allocate another vehicle."""
    boundary, path = ProductBoundary(), tmp_path / "examples.json"
    boundary.fail_after = operation
    with pytest.raises(ConnectionError):
        provision(boundary, path)
    provision(boundary, path)
    assert {"owns:consumers", "consumers"} <= set(examples.ExampleJournal(path).state["completed"])
    assert len(boundary.devices) == 3
    assert all(
        row["profile"]["samples"] == (1 if "dryer" in entity else 2)
        for entity, row in boundary.devices.items()
    )
    assert len({row["id"] for row in boundary.ev["vehicles"]}) == 2
    assert len(boundary.thermal["stores"]) == 1


@pytest.mark.parametrize(
    "section,path",
    [
        ("consumers", "/v1/consumers"),
        ("ev", "/v1/ev/configuration"),
        ("thermal", "/v1/thermal/configuration"),
    ],
)
def test_concurrent_user_save_is_not_claimed_by_provisioning(tmp_path, section, path):
    """An explicit version rejection preserves the user's winning configuration without blueprint ownership."""
    boundary = ProductBoundary()
    journal_path = tmp_path / "examples.json"
    rejected = False

    async def request(method, route, body=None):
        """Save user settings between read and write, then reject the provisioner's stale version."""
        nonlocal rejected
        if method != "GET" and route == path and not rejected:
            rejected = True
            if section == "consumers":
                # Even identical forecast selections do not grant permission to shift appliances.
                boundary.selected = deepcopy(body["selected"])
            elif section == "ev":
                boundary.ev = {"vehicles": [{"id": "user-car"}], "points": []}
            else:
                boundary.thermal = {"stores": [{"id": "user-boiler"}]}
            raise ClientResponseError(SimpleNamespace(real_url="http://app" + path), (), status=409)
        return await boundary.request(method, route, body)

    with pytest.raises(ClientResponseError):
        asyncio.run(examples.configure(request, examples.ExampleJournal(journal_path), NOW))
    state = examples.ExampleJournal(journal_path).state
    assert "owns:" + section not in state["completed"]
    assert "attempted:" + section not in state["completed"]
    asyncio.run(examples.configure(request, examples.ExampleJournal(journal_path), NOW))
    assert "owns:" + section not in examples.ExampleJournal(journal_path).state["completed"]
    if section == "consumers":
        assert boundary.devices == {}
    elif section == "ev":
        assert boundary.ev == {"vehicles": [{"id": "user-car"}], "points": []}
    else:
        assert boundary.thermal == {"stores": [{"id": "user-boiler"}]}


@pytest.mark.parametrize(
    "section,path", [("ev", "/v1/ev/configuration"), ("thermal", "/v1/thermal/configuration")]
)
def test_ambiguous_write_does_not_claim_unrelated_user_identities(tmp_path, section, path):
    """An unknown response followed by unrelated user settings cannot authorize bootstrap blueprint identities."""
    boundary = ProductBoundary()
    journal_path = tmp_path / "examples.json"
    boundary.fail_after = path
    with pytest.raises(ConnectionError):
        provision(boundary, journal_path)
    if section == "ev":
        boundary.ev = {"vehicles": [{"id": "user-car"}], "points": []}
    else:
        boundary.thermal = {"stores": [{"id": "user-boiler"}]}
    provision(boundary, journal_path)
    assert "owns:" + section not in examples.ExampleJournal(journal_path).state["completed"]


@pytest.mark.parametrize(
    "section,category,field,value",
    [
        ("ev", "vehicles", "target_soc", 70),
        ("ev", "points", "energy_entity", "sensor.user_wallbox_energy"),
        ("ev", "points", "maximum_w", 6000),
        ("thermal", "stores", "temperature_entity", "sensor.user_boiler_temperature"),
        ("thermal", "stores", "heater_w", 1000),
    ],
)
def test_ambiguous_write_retains_intervening_edits_without_blueprint_ownership(
    tmp_path, section, category, field, value
):
    """Keeping bootstrap UUIDs does not authorize new automations after source, limit or intent edits."""
    boundary = ProductBoundary()
    journal_path = tmp_path / "examples.json"
    boundary.fail_after = "/v1/" + section + "/configuration"
    with pytest.raises(ConnectionError):
        provision(boundary, journal_path)
    saved = boundary.ev if section == "ev" else boundary.thermal
    saved[category][0][field] = value
    before = deepcopy(saved)
    provision(boundary, journal_path)
    assert (boundary.ev if section == "ev" else boundary.thermal) == before
    assert "owns:" + section not in examples.ExampleJournal(journal_path).state["completed"]


def test_existing_user_choices_are_not_converted_into_shift_permissions(tmp_path):
    """Existing forecast selections, cycle tasks, EV settings and boiler settings are authoritative."""
    boundary = ProductBoundary()
    boundary.selected = [
        {"entity_id": "sensor.oep_test_dishwasher_energy", "weather_sensitive": True}
    ]
    boundary.devices = {
        "sensor.oep_test_dishwasher_energy": {
            "entity_id": "sensor.oep_test_dishwasher_energy",
            "shiftable": False,
            "profile": {"user": "retain"},
            "task": {"status": "running"},
        }
    }
    boundary.ev = {"vehicles": [{"user": "retain"}], "points": []}
    boundary.thermal = {"stores": [{"user": "retain"}]}
    before = deepcopy((boundary.selected, boundary.devices, boundary.ev, boundary.thermal))
    provision(boundary, tmp_path / "examples.json")
    assert before == (boundary.selected, boundary.devices, boundary.ev, boundary.thermal)
    assert all(method == "GET" for method, _, _ in boundary.calls)


def test_partial_foundation_and_missing_fixture_are_not_backfilled(tmp_path, monkeypatch):
    """An interrupted wizard is deferred and a foreign Energy catalogue fails before mutations."""
    delays = []

    async def sleep(seconds):
        """Record bounded polling without delaying the regression suite."""
        delays.append(seconds)

    monkeypatch.setattr(examples.asyncio, "sleep", sleep)
    boundary = ProductBoundary()
    boundary.setup["installation"] = None
    provision(boundary, tmp_path / "partial.json")
    assert all(method == "GET" for method, _, _ in boundary.calls)
    boundary.setup["installation"] = {"saved": True}
    boundary.options.pop()
    with pytest.raises(ValueError, match="complete simulated Energy fixture"):
        provision(boundary, tmp_path / "foreign.json")
    assert not boundary.devices and not boundary.selected
    assert delays == [2] * 59


def test_late_fixture_discovery_is_waited_for_before_configuration(tmp_path, monkeypatch):
    """Two incomplete catalogue responses are harmless. Configure only after every simulator source arrives."""
    boundary = ProductBoundary()
    polls, delays = [], []

    async def request(method, path, body=None):
        """Expose delayed Energy discovery while retaining the actual product validation boundary."""
        response = await boundary.request(method, path, body)
        if method == "GET" and path == "/v1/consumers":
            polls.append(True)
            if len(polls) <= 2:
                response["options"] = response["options"][:-1]
        return response

    async def sleep(seconds):
        """Advance the scenario by another discovery response without a real timer."""
        delays.append(seconds)
        assert not boundary.devices and not boundary.selected

    monkeypatch.setattr(examples.asyncio, "sleep", sleep)
    asyncio.run(
        examples.configure(request, examples.ExampleJournal(tmp_path / "examples.json"), NOW)
    )
    assert delays == [2, 2] and len(polls) == 3
    assert len(boundary.devices) == 3 and len(boundary.ev["vehicles"]) == 2


@pytest.mark.parametrize("refresh_cycles", [1, 2])
def test_cached_partial_catalogue_requires_explicit_refresh(tmp_path, monkeypatch, refresh_cycles):
    """Late HA devices stay absent from the cached app catalogue until discovery is requested."""
    boundary = ProductBoundary()
    refreshes = 0
    delays = []

    async def request(method, path, body=None):
        """Publish the completed fixture on the next integration poll after a refresh request."""
        nonlocal refreshes
        response = await boundary.request(method, path, body)
        if path == "/v1/setup?refresh=1":
            refreshes += 1
        if (
            method == "GET"
            and path == "/v1/consumers"
            and (refreshes < refresh_cycles or len(delays) < refresh_cycles)
        ):
            response["options"] = []
        return response

    async def sleep(seconds):
        """An ordinary wait cannot refresh an otherwise fresh five-minute catalogue."""
        assert not boundary.devices and not boundary.selected
        delays.append(seconds)

    monkeypatch.setattr(examples.asyncio, "sleep", sleep)
    asyncio.run(
        examples.configure(request, examples.ExampleJournal(tmp_path / "examples.json"), NOW)
    )
    assert delays == [2] * refresh_cycles
    assert refreshes == refresh_cycles
    assert len(boundary.devices) == 3 and len(boundary.ev["vehicles"]) == 2


@pytest.mark.parametrize(
    "entity",
    [
        "sensor.oep_test_car_a_soc",
        "sensor.oep_test_car_b_soc",
        "sensor.oep_test_wallbox_a_power",
        "sensor.oep_test_wallbox_b_power",
        "binary_sensor.oep_test_wallbox_a_connected",
        "binary_sensor.oep_test_wallbox_b_connected",
        "sensor.oep_test_boiler_power",
        "sensor.oep_test_boiler_temperature",
    ],
)
def test_energy_membership_does_not_admit_missing_device_metadata(tmp_path, monkeypatch, entity):
    """All Energy meters can precede their states. No settings may be written until both arrive."""
    boundary = ProductBoundary()
    metadata = boundary.candidates["entities"]
    delayed = next(row for row in metadata if row["entity_id"] == entity)
    metadata.remove(delayed)
    refreshed = False
    ready = False
    scope = "thermal" if "boiler" in entity else "ev"
    choice_path = "/v1/ha/source-choices/" + scope

    async def request(method, path, body=None):
        """Register a refresh request without immediately completing the integration poll."""
        nonlocal refreshed
        if method == "POST" and path == choice_path:
            refreshed = True
        result = await boundary.request(method, path, body)
        if method == "GET" and path == choice_path and refreshed and not ready:
            return {"pending": True, "failed": False, "catalogue": None}
        return result

    async def sleep(_seconds):
        """Publish late metadata only after refresh and prove no earlier configuration mutation."""
        nonlocal ready
        assert refreshed
        assert all(
            method == "GET" or path.startswith("/v1/ha/source-choices/")
            for method, path, _ in boundary.calls
        )
        metadata.append(delayed)
        boundary.choice_results[scope]["entities"].append(deepcopy(delayed))
        ready = True

    monkeypatch.setattr(examples.asyncio, "sleep", sleep)
    asyncio.run(
        examples.configure(request, examples.ExampleJournal(tmp_path / "examples.json"), NOW)
    )
    assert refreshed
    assert sum(method == "POST" and path == choice_path for method, path, _ in boundary.calls) == 1
    assert len(boundary.devices) == 3 and len(boundary.ev["vehicles"]) == 2


@pytest.mark.parametrize("scope", ["ev", "thermal"])
@pytest.mark.parametrize("outcome", ["failed", "pending", "incomplete", "malformed"])
def test_choice_failure_never_writes_settings_or_claims_examples(
    tmp_path, monkeypatch, scope, outcome
):
    """One bounded query rejects failed, missing or ambiguous results before any product settings change."""
    boundary = ProductBoundary()
    journal_path = tmp_path / "examples.json"
    choice_path = "/v1/ha/source-choices/" + scope
    requested = False
    delays = []
    monkeypatch.setattr(examples, "FIXTURE_READY_ATTEMPTS", 3)

    async def request(method, path, body=None):
        """Expose only the chosen failed boundary after the explicit request was accepted."""
        nonlocal requested
        result = await boundary.request(method, path, body)
        if method == "POST" and path == choice_path:
            requested = True
        if method == "GET" and path == choice_path and requested:
            return (
                {
                    "pending": outcome == "pending",
                    "failed": outcome == "failed",
                    "catalogue": None,
                }
                if outcome != "malformed"
                else {}
            )
        return result

    async def sleep(seconds):
        """Record finite waiting without delaying the suite or permitting settings writes."""
        delays.append(seconds)
        assert not boundary.devices and not boundary.selected

    monkeypatch.setattr(examples.asyncio, "sleep", sleep)
    with pytest.raises(ValueError, match="Development example source choices") as failure:
        asyncio.run(examples.configure(request, examples.ExampleJournal(journal_path), NOW))
    if outcome == "failed":
        assert str(failure.value) == (
            "Development example source choices failed or timed out. "
            "Check the HA connection and fixture sources, then retry provisioning"
        )
    assert not journal_path.exists()
    assert not boundary.devices and not boundary.selected and not boundary.ev["vehicles"]
    assert not boundary.thermal["stores"]
    assert all(
        method == "GET" or path.startswith("/v1/ha/source-choices/")
        for method, path, _ in boundary.calls
    )
    assert sum(method == "POST" and path == choice_path for method, path, _ in boundary.calls) == 1
    assert delays == ([2, 2] if outcome in ("pending", "malformed") else [])


def test_choices_expired_during_profile_work_are_refreshed_before_save(tmp_path):
    """Long example learning cannot save against expired editor choices or enlarge active metadata."""
    boundary = ProductBoundary()
    expired = False

    async def request(method, path, body=None):
        """Expire both accepted results once while actual profile validation continues."""
        nonlocal expired
        result = await boundary.request(method, path, body)
        if path == "/v1/appliances/history" and not expired:
            boundary.choice_results.clear()
            expired = True
        return result

    asyncio.run(
        examples.configure(request, examples.ExampleJournal(tmp_path / "examples.json"), NOW)
    )
    assert expired
    for scope in ("ev", "thermal"):
        assert (
            sum(
                method == "POST" and path == "/v1/ha/source-choices/" + scope
                for method, path, _ in boundary.calls
            )
            == 2
        )
    assert len(boundary.ev["vehicles"]) == 2 and len(boundary.thermal["stores"]) == 1


@pytest.mark.parametrize("scope", ["ev", "thermal"])
def test_existing_pending_choice_is_reused(scope):
    """A prior in-flight query completes without another POST or nonce replacement."""
    boundary = ProductBoundary()
    choice_path = "/v1/ha/source-choices/" + scope

    async def scenario():
        """Reuse the accepted request across a fresh caller while observing genuine completion."""
        await boundary.request("POST", choice_path, {})
        boundary.calls.clear()
        reads = 0

        async def request(method, path, body=None):
            """Expose an existing pending response once before its complete result."""
            nonlocal reads
            result = await boundary.request(method, path, body)
            if method == "GET" and path == choice_path:
                reads += 1
                if reads == 1:
                    return {"pending": True, "failed": False, "catalogue": None}
            return result

        required = {entity["entity_id"] for entity in boundary.choice_results[scope]["entities"]}
        await examples.ensure_source_choices(request, scope, required)
        assert reads == 2
        assert all(method == "GET" for method, _, _ in boundary.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "section,path",
    [
        ("consumers", "/v1/consumers"),
        ("ev", "/v1/ev/configuration"),
        ("thermal", "/v1/thermal/configuration"),
    ],
)
def test_removal_after_ambiguous_write_is_not_recreated(tmp_path, section, path):
    """An empty result after a lost successful response is preserved instead of guessing whether the user removed it."""
    boundary = ProductBoundary()
    if section == "consumers":
        boundary.ev = {"vehicles": [{"id": "user-car"}], "points": []}
        boundary.thermal = {"stores": [{"id": "user-boiler"}]}
    journal_path = tmp_path / "examples.json"
    boundary.fail_after = path
    with pytest.raises(ConnectionError):
        provision(boundary, journal_path)
    if section == "consumers":
        boundary.selected = []
    elif section == "ev":
        boundary.ev = {"vehicles": [], "points": []}
    else:
        boundary.thermal = {"stores": []}
    boundary.calls.clear()
    provision(boundary, journal_path)
    assert not any(method != "GET" and route == path for method, route, _ in boundary.calls)
    assert "owns:" + section not in examples.ExampleJournal(journal_path).state["completed"]
    if section == "consumers":
        assert boundary.selected == [] and boundary.devices == {}
    elif section == "ev":
        assert boundary.ev == {"vehicles": [], "points": []}
    else:
        assert boundary.thermal == {"stores": []}


def test_zonneplan_example_uses_separate_conditions_cap_and_ha_calendar(tmp_path):
    """At market .18, export pays .20 plus .02 only during daylight and before the 7500-kWh cap."""
    document = examples.zonneplan_contract("Europe/Amsterdam", NOW)
    contract = parse_contract(document, NOW, installation_timezone="Europe/Amsterdam")
    bonus = contract.bonuses[0]
    assert document["periods"][0]["contract_start"] == "2026-10-01"
    assert contract.periods[0].effective_from.hour == 22
    assert contract.periods[0].export_formula.evaluate(0.18) == pytest.approx(0.20)
    assert contract.periods[0].import_formula.evaluate(0.1) == pytest.approx(0.2518481)
    assert bonus.extra_rate(0.18, NOW, check_window=False) == pytest.approx(0.02)
    assert bonus.extra_rate(-0.03, NOW, check_window=False) == 0
    assert bonus.extra_rate(-0.02, NOW, check_window=False) == 0
    assert bonus.extra_rate(-0.01, NOW, check_window=False) == pytest.approx(0.001)
    assert bonus.window == "daylight" and bonus.cap_kwh == 7500
    assert (bonus.reset_month, bonus.reset_day, bonus.timezone) == (1, 1, "Europe/Amsterdam")


def test_only_exact_unchanged_old_sample_contract_gains_the_bonus(tmp_path):
    """A previous default upgrades once. Any changed formula or existing bonus is preserved."""
    boundary = ProductBoundary()
    old = {
        "contract_id": "interactive-test-only",
        "market_area": "NL",
        "currency": "EUR",
        "periods": [
            {
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_until": None,
                "import_formula": "(market + 0.10) * 1.21",
                "export_formula": "market - 0.02",
            }
        ],
    }
    boundary.contract = contract_document(parse_contract(old, NOW))
    provision(boundary, tmp_path / "old.json")
    assert len(boundary.contract["bonuses"]) == 1
    before = deepcopy(boundary.contract)
    provision(boundary, tmp_path / "old.json")
    assert boundary.contract == before
    changed = ProductBoundary()
    old["periods"][0]["export_formula"] = "market - 0.03"
    changed.contract = contract_document(parse_contract(old, NOW))
    before = deepcopy(changed.contract)
    provision(changed, tmp_path / "custom.json")
    assert changed.contract == before


@pytest.mark.parametrize("change", ["rounding", "user_edit", "open_task", "manual"])
def test_original_bootstrap_profile_upgrade_preserves_user_changes(tmp_path, change):
    """Only original single synthetic samples tolerate rounding during the one-time detector upgrade."""
    boundary, path = ProductBoundary(), tmp_path / "examples.json"
    provision(boundary, path)
    journal = examples.ExampleJournal(path)
    journal.state["completed"] = [
        key for key in journal.state["completed"] if not key.startswith("automatic-learning-v1:")
    ]
    journal.save()
    powers = [value + 1e-9 for value in (1800, 80, 80, 0, 80, 1200, 80, 0)]
    device = boundary.devices["sensor.oep_test_dishwasher_energy"]
    device["profile"] = {"power_w": powers, "samples": 1, "evidence": "synthetic"}
    if change == "user_edit":
        device["profile"]["power_w"][0] += 10
    elif change == "open_task":
        device["task"] = {"status": "running"}
    elif change == "manual":
        device["manual_profile"] = {"duration_minutes": 90, "energy_kwh": 0.6}
    before = deepcopy(device)
    boundary.calls.clear()
    provision(boundary, path)
    if change == "rounding":
        assert device["profile"]["samples"] == 2
        assert len(device["profile"]["power_w"]) == 7
    else:
        assert device == before
        assert not any(
            method == "POST" and body.get("entity_id") == "sensor.oep_test_dishwasher_energy"
            for method, _route, body in boundary.calls
        )
