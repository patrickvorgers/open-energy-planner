"""Local command admission uses app estimates without hiding newer actual vehicle measurements."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.open_energy_planner import ev
from tests.app.ev.support.devices import settings

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "value, kind, observed, expected",
    [
        (None, None, None, 65),
        (50, "ha_report", NOW, 65),
        (80, "ha_report", NOW, 80),
        (50, "device_sample", NOW, 50),
        (80, "device_sample", NOW - timedelta(days=2), 65),
    ],
)
def test_effective_soc_uses_estimate_unless_real_local_sample_supersedes_it(
    monkeypatch, value, kind, observed, expected
):
    """An unavailable or republished anchor retains 65%. A newer actual 80% stops below-target permission."""
    vehicle = {
        "actual": {
            "soc": 65,
            "measured_soc": 50,
            "observed_at": (NOW - timedelta(days=1)).isoformat(),
        }
    }
    reading = (
        SimpleNamespace(value=value, timestamp_kind=kind, observed_at=observed)
        if value is not None
        else None
    )
    monkeypatch.setattr(ev, "soc_reading", lambda *_args: reading)
    assert ev.soc_evidence(None, vehicle, NOW)["soc"] == expected


def test_vehicle_status_keeps_original_time_without_private_location():
    """Only Home/Away and original provider sample timestamps cross the app boundary."""
    old = NOW - timedelta(days=1)
    state = SimpleNamespace(
        state="home",
        attributes={"position_updated_at": old.isoformat(), "latitude": 12, "longitude": 34},
        last_updated=NOW,
        last_reported=NOW,
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity: state))
    result = ev.vehicle_status(hass, "device_tracker.synthetic_car", NOW, location=True)
    assert result == {
        "value": True,
        "observed_at": old.isoformat(),
        "reported_at": NOW.isoformat(),
        "timestamp_kind": "device_sample",
    }


def test_capture_preserves_meter_observation_time(monkeypatch):
    """A current HA packet retains a one-minute-old meter observation instead of retiming it."""
    observed = NOW - timedelta(minutes=1)
    monkeypatch.setattr(
        ev, "energy_reading", lambda *_args: SimpleNamespace(value=100, observed_at=observed)
    )
    monkeypatch.setattr(ev, "power_reading", lambda *_args: SimpleNamespace(value=0))
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(
            states=SimpleNamespace(get=lambda _entity: SimpleNamespace(state="on"))
        ),
        ev_sessions={},
    )
    point = {"id": "point", "connected_entity": "binary_sensor.synthetic_plug"}
    captured = ev.capture(coordinator, {"configuration_version": "version", "points": [point]}, NOW)
    assert captured["points"][0]["energy_kwh"] == 100
    assert captured["points"][0]["energy_observed_at"] == observed.isoformat()
    assert captured["at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "field, value",
    [
        ("soc", True),
        ("soc", float("nan")),
        ("soc", 101),
        ("source_entity", "sensor.other_car"),
        ("basis", "unproven"),
        ("observed_at", "invalid"),
    ],
)
def test_projection_rejects_invalid_soc_before_command_admission(field, value):
    """Nonfinite, misbound and malformed app estimates cannot enter native command state."""
    projection = {**settings(), "configuration_version": "configuration", "revision": "revision"}
    for vehicle in projection["vehicles"]:
        vehicle.update(name="Synthetic car", next_departure=(NOW + timedelta(hours=1)).isoformat())
    for point in projection["points"]:
        point.update(name="Synthetic wallbox", actual={})
    actual = {
        "source_entity": "sensor.car_soc",
        "basis": "last_known",
        "soc": 50,
        "measured_soc": 50,
        "observed_at": NOW.isoformat(),
        "estimate_updated_at": NOW.isoformat(),
    }
    actual[field] = value
    projection["vehicles"][0]["actual"] = actual
    with pytest.raises((ValueError, TypeError)):
        ev.receive(projection)
