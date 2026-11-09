"""Independent automatic detection, fallback, persistence and sensor-ownership evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles.learning import detect_cycles, manual_profile
from open_energy_planner.appliances.cycles.service import ApplianceService

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
ENTITY = "sensor.washer_energy"
POWER = "sensor.washer_power"


def readings(*, cycles=2):
    """Each 75-minute cycle consumes .5 kWh, with a fifteen-minute zero-power pause.

    Three five-minute bins at 1000 W, three at zero and nine at 333 1/3 W
    total .25 + .25 = .5 kWh. Quiet brackets never enter the measured profile.
    """
    start, total, rows = NOW - timedelta(days=2), 10.0, []
    powers = [0] * 12 + [1000] * 3 + [0] * 3 + [1000 / 3] * 9 + [0] * 12
    for index, power in enumerate(powers * cycles):
        total += power / 12000
        rows.append(
            {
                "at": (start + timedelta(minutes=index * 5)).isoformat(),
                "power_w": power,
                "kwh": total,
            }
        )
    return rows


def test_detects_complete_energy_conserving_cycles_with_internal_pauses(service):
    """Two real detected ranges admit a .5-kWh profile without granting readiness or shift permission."""
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    row = service.document()["devices"][0]
    assert row["learning_status"] == "ready" and row["profile_kind"] == "Learned"
    assert row["profile"]["samples"] == 2
    assert row["profile"]["power_w"] == pytest.approx([1000, 0, 1000 / 3, 1000 / 3, 1000 / 3])
    assert [cycle["energy_kwh"] for cycle in row["recent_cycles"]] == pytest.approx([0.5, 0.5])
    assert not row["shiftable"] and row["task"] is None
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    restored = ApplianceService(service.documents, service.consumers)
    assert restored.document() == json.loads(json.dumps(service.document()))
    assert restored.document()["devices"][0]["profile"]["samples"] == 2


def test_negative_recorder_origins_learn_identical_energy_and_still_reject_resets(service):
    """A negative arbitrary Recorder sum origin changes no cycle kWh or reset detection."""
    rows = readings()
    for row in rows:
        row["kwh"] -= 100
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    saved = service.document()["devices"][0]
    assert saved["profile"]["samples"] == 2
    assert [cycle["energy_kwh"] for cycle in saved["recent_cycles"]] == pytest.approx([0.5, 0.5])
    reset = rows[:39]
    for row in reset[20:]:
        row["kwh"] -= 2
    assert detect_cycles(reset, NOW) == []


def test_one_invalid_energy_cycle_does_not_block_later_complete_evidence(service):
    """A flat low-resolution counter in the first run is ignored while two later .5-kWh cycles learn."""
    rows = readings(cycles=3)
    end_of_first = 39
    first_origin = rows[0]["kwh"]
    for row in rows[:end_of_first]:
        row["kwh"] = first_origin
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    saved = service.document()["devices"][0]
    assert saved["profile"]["samples"] == 2
    assert [cycle["energy_kwh"] for cycle in saved["recent_cycles"]] == pytest.approx([0.5, 0.5])


def test_manual_fallback_survives_learning_and_accepted_tasks_stay_frozen(service):
    """A .8-kWh, 100-minute estimate survives two detected cycles and cannot change an accepted task."""
    service.set_manual_profile(ENTITY, 100, 0.8, service.revision)
    assert sum(service.state["devices"][ENTITY]["profile"]["power_w"]) / 4000 == pytest.approx(0.8)
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    assert service.document()["devices"][0]["profile_kind"] == "Manual"
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "ready", NOW, NOW + timedelta(hours=4), NOW)
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    row = service.document()["devices"][0]
    assert row["profile_kind"] == "Learned"
    assert row["manual_profile"] == {"duration_minutes": 100, "energy_kwh": 0.8}
    assert row["task"]["profile"] == task["profile"]
    assert sum(row["task"]["profile"]["power_w"]) / 4000 == pytest.approx(0.8)


@pytest.mark.parametrize("kind", ["gap", "reset", "missing_power", "missing_energy", "unfinished"])
def test_uncertain_history_never_creates_a_complete_cycle(kind):
    """Gaps, resets, absent values and missing closing quiet evidence cannot invent a completed run."""
    rows = readings(cycles=1)
    if kind == "gap":
        rows.pop(20)
    elif kind == "reset":
        for row in rows[20:]:
            row["kwh"] -= 5
    elif kind == "missing_power":
        rows[20]["power_w"] = None
    elif kind == "missing_energy":
        rows[20]["kwh"] = None
    else:
        rows = rows[:28]
    assert detect_cycles(rows, NOW) == []


def test_source_change_withdraws_learned_profile_and_retains_manual(service):
    """Energy sensor relinking invalidates learned evidence without losing the explicit fallback or archives."""
    service.set_manual_profile(ENTITY, 90, 0.6, service.revision)
    service.learn_history(ENTITY, POWER, readings(), True, NOW)
    service.consumers.options = ({"entity_id": ENTITY, "name": "Washer", "power_entity": None},)
    service.sync_sources()
    row = service.document()["devices"][0]
    assert row["profile_kind"] == "Manual" and row["learning_status"] == "missing_power_sensor"
    with pytest.raises(ValueError, match="current Home Assistant"):
        service.learn_history(ENTITY, POWER, readings(), False, NOW)


@pytest.mark.parametrize(
    "duration,energy", [(0, 1), (721, 1), (100, 0), (True, 1), (90, float("nan")), (15, 100)]
)
def test_manual_profile_rejects_invalid_physical_inputs(duration, energy):
    """Invalid duration, energy, booleans and impossible average power fail at the domain boundary."""
    with pytest.raises(ValueError):
        manual_profile(duration, energy)


def test_manual_profile_requires_current_saved_revision(service):
    """Concurrent configuration changes refuse a stale manual estimate without changing its saved values."""
    revision = service.revision
    service.set_manual_profile(ENTITY, 90, 0.6, revision)
    with pytest.raises(ValueError, match="settings changed"):
        service.set_manual_profile(ENTITY, 100, 1, revision)
    assert service.document()["devices"][0]["manual_profile"]["energy_kwh"] == 0.6


def test_retained_records_do_not_exhaust_active_appliance_capacity(service):
    """Seventeen successive one-device selections retain old profiles but expose only the current appliance."""
    options = [
        {"entity_id": f"sensor.device_{index}_energy", "upstream": None} for index in range(17)
    ]
    service.consumers.sync({"entities": [], "suggestions": {"consumers": options}})
    for option in options:
        service.consumers.save(
            [{"entity_id": option["entity_id"], "weather_sensitive": False}],
            service.consumers.version,
        )
        service.configure(option["entity_id"], True)
        service.set_manual_profile(option["entity_id"], 60, 0.1, service.revision)
    assert len(service.state["devices"]) == 17
    assert service.state["devices"]["sensor.device_0_energy"]["manual_profile"]["energy_kwh"] == 0.1
    assert [row["entity_id"] for row in service.document()["devices"]] == [
        "sensor.device_16_energy"
    ]


def test_domain_preserves_more_than_sixteen_active_appliances(service):
    """All seventeen explicitly selected appliances retain independent configuration and identity."""
    options = [
        {"entity_id": f"sensor.device_{index}_energy", "upstream": None} for index in range(17)
    ]
    service.consumers.sync({"entities": [], "suggestions": {"consumers": options}})
    service.consumers.selected = tuple(
        {"entity_id": option["entity_id"], "weather_sensitive": False} for option in options
    )
    for option in options:
        service.configure(option["entity_id"], True)
    assert len(service.state["devices"]) == 17
    assert {row["entity_id"] for row in service.document()["devices"]} == {
        option["entity_id"] for option in options
    }


def test_http_profile_and_history_writes_preserve_contract_and_skip_unchanged_refresh(service):
    """Real HTTP writes retain manual/learned provenance and duplicate history does not request another plan."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.appliances.cycles.api import register

    async def scenario():
        """Exercise route parsing and serialized SQL writes against the actual appliance service."""
        app, refreshes = web.Application(), []
        register(
            app,
            service,
            SimpleNamespace(state={"enabled": False, "plan": None}),
            SimpleNamespace(refresh=lambda **kwargs: refreshes.append(kwargs)),
            asyncio.Lock(),
            SimpleNamespace(state={"points": []}),
            SimpleNamespace(state={"stores": []}),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/appliances/manual_profile",
                json={
                    "entity_id": ENTITY,
                    "duration_minutes": 90,
                    "energy_kwh": 0.6,
                    "expected_version": service.revision,
                },
            )
            assert response.status == 200
            assert (await response.json())["appliances"]["devices"][0]["profile_kind"] == "Manual"
            stale = service.revision
            service.set_manual_profile(ENTITY, 100, 0.8, stale)
            response = await client.post(
                "/v1/appliances/device",
                json={"entity_id": ENTITY, "shiftable": True, "expected_version": stale},
            )
            assert response.status == 400 and "settings changed" in await response.text()
            assert service.document()["devices"][0]["shiftable"] is False
            response = await client.post(
                "/v1/appliances/device",
                json={"entity_id": ENTITY, "shiftable": True, "expected_version": service.revision},
            )
            assert response.status == 200
            saved_revision = (await response.json())["appliances"]["revision"]
            service.set_manual_profile(ENTITY, 120, 1.2, saved_revision)
            response = await client.post(
                "/v1/appliances/manual_profile",
                json={
                    "entity_id": ENTITY,
                    "duration_minutes": 90,
                    "energy_kwh": 0.6,
                    "expected_version": saved_revision,
                },
            )
            assert response.status == 400
            assert service.document()["devices"][0]["manual_profile"]["energy_kwh"] == 1.2
            rows = readings(cycles=3)
            rows[13]["power_w"] = 20001
            shift = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - NOW
            for row in rows:
                row["at"] = (datetime.fromisoformat(row["at"]) + shift).isoformat()
            body = {"entity_id": ENTITY, "power_entity": POWER, "rows": rows, "synthetic": True}
            response = await client.post("/v1/appliances/history", json=body)
            assert response.status == 200, await response.text()
            saved = (await response.json())["appliances"]["devices"][0]
            assert saved["profile_kind"] == "Learned" and saved["profile"]["samples"] == 2
            assert saved["profile"]["evidence"] == "synthetic"
            count = len(refreshes)
            response = await client.post("/v1/appliances/history", json=body)
            assert response.status == 200 and len(refreshes) == count
            response = await client.post(
                "/v1/appliances/history", json={**body, "power_entity": "sensor.wrong"}
            )
            assert response.status == 400
            assert service.document()["devices"][0]["profile"]["samples"] == 2

    asyncio.run(scenario())
