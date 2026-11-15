"""Durable autonomous arrival requests and SoC evidence on the shared database matrix."""

from datetime import timedelta
from uuid import uuid4

import pytest

from open_energy_planner.appliances.ev.service import EVService
from tests.app.ev.support.devices import CAR, NOW, POINT, catalogue, observations, settings
from tests.database.support import ev as ev_support

ev_service = ev_support.ev_service


def single_car(service):
    """Allow one car without requiring optional vehicle provider signals."""
    configuration = settings()
    configuration["points"][0]["vehicle_ids"] = [CAR]
    service.configure(configuration, catalogue(), NOW)


def test_arrival_creates_one_request_and_cancellation_survives_restart(ev_service):
    """Arrival uses defaults once. Polling and app restart cannot undo the user's cancellation."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    request = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    assert request["target_soc"] == 80
    assert request["departure"] == "2026-10-27T06:00:00+00:00"
    assert ev_service.state["assignments"][POINT]["vehicle_id"] == CAR
    assert ev_service.work(NOW, "Europe/Amsterdam")[0].required_kwh == 18
    ev_service.cancel_request(request["id"], request["version"], NOW, CAR)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    later = NOW + timedelta(seconds=10)
    restored.receive(observations(restored, later), later, "Europe/Amsterdam")
    assert restored.work(later, "Europe/Amsterdam") == ()
    assert len(restored.state["requests"]) == 1
    assert restored.state["requests"][request["id"]]["status"] == "cancelled"


def test_meter_estimate_survives_restart_without_reviving_stale_connection(ev_service):
    """2.3 measured AC kWh gives 1.84 battery kWh. Restart retains it but requires a fresh session report."""
    single_car(ev_service)
    first = observations(ev_service)
    first["points"][0]["energy_kwh"] = 100
    ev_service.receive(first, NOW, "Europe/Amsterdam")
    later = NOW + timedelta(hours=1)
    report = observations(ev_service, later, soc=None)
    report["points"][0]["energy_kwh"] = 102.3
    ev_service.receive(report, later, "Europe/Amsterdam")
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    estimate = restored.vehicle_readings(later)[CAR]
    assert estimate["soc"] == pytest.approx(50 + 1.84 / 60 * 100)
    assert estimate["observed_at"] == NOW.isoformat()
    assert estimate["basis"] == "estimated_from_meter"
    assert restored.work(later, "Europe/Amsterdam") == ()
    report["at"] = (later + timedelta(seconds=1)).isoformat()
    restored.receive(report, later + timedelta(seconds=1), "Europe/Amsterdam")
    assert restored.work(later + timedelta(seconds=1), "Europe/Amsterdam")[
        0
    ].initial_soc == pytest.approx(estimate["soc"])


def test_failed_observation_write_preserves_memory_and_durable_telemetry(ev_service, monkeypatch):
    """A failed commit cannot publish a new SoC anchor in memory or after reconstruction."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    original_write = ev_service.documents.write

    def fail_telemetry(key, value):
        """Fail only the mutable telemetry pointer after preceding writes in this transaction."""
        if key == "ev-telemetry.json":
            raise RuntimeError("Synthetic storage failure")
        return original_write(key, value)

    monkeypatch.setattr(ev_service.documents, "write", fail_telemetry)
    later = NOW + timedelta(seconds=5)
    with pytest.raises(RuntimeError, match="Synthetic"):
        ev_service.receive(observations(ev_service, later, soc=60), later, "Europe/Amsterdam")
    assert ev_service.vehicle_readings(later)[CAR]["soc"] == 50
    assert ev_service.report["at"] == NOW.isoformat()
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert restored.vehicle_readings(later)[CAR]["soc"] == 50


@pytest.mark.parametrize(
    "interruption", ["configuration", "soc_source_change", "identity_loss", "identity_loss_restart"]
)
def test_cancelled_manual_request_does_not_restart_on_same_physical_connection(
    ev_service, interruption
):
    """Unrelated settings or temporary ambiguity cannot undo an explicitly cancelled manual request."""
    configuration = settings()
    configuration["points"][0]["vehicle_ids"] = [CAR]
    configuration["vehicles"][0]["connected_entity"] = "binary_sensor.synthetic_car_plug"
    sources = catalogue()
    sources["entities"].append(
        {
            "entity_id": "binary_sensor.synthetic_car_plug",
            "name": "Car plug",
            "unit": "",
            "device_class": "plug",
        }
    )
    ev_service.configure(configuration, sources, NOW)
    request_id = str(uuid4())
    ev_service.save_request({"request_id": request_id, "vehicle_id": CAR}, NOW, "Europe/Amsterdam")
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    request = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    assert request["id"] == request_id
    ev_service.cancel_request(request_id, request["version"], NOW, CAR)
    if interruption in ("configuration", "soc_source_change"):
        configuration["vehicles"][0]["target_soc"] = 85
        if interruption == "soc_source_change":
            configuration["vehicles"][0]["soc_entity"] = "sensor.synthetic_replacement_soc"
            sources["entities"].append(
                {
                    "entity_id": "sensor.synthetic_replacement_soc",
                    "name": "Replacement SoC",
                    "unit": "%",
                    "device_class": "battery",
                }
            )
        ev_service.configure(configuration, sources, NOW)
    else:
        at = NOW + timedelta(seconds=5)
        report = observations(ev_service, at)
        report["vehicles"][0]["connected"] = {
            "value": False,
            "observed_at": at.isoformat(),
            "reported_at": at.isoformat(),
            "timestamp_kind": "device_sample",
        }
        ev_service.receive(report, at, "Europe/Amsterdam")
        assert ev_service.state["assignments"][POINT]["vehicle_id"] is None
    if interruption == "identity_loss_restart":
        ev_service = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    later = NOW + timedelta(seconds=10)
    ev_service.receive(observations(ev_service, later), later, "Europe/Amsterdam")
    assert ev_service.work(later, "Europe/Amsterdam") == ()
    assert ev_service.state["active_requests"] == {}
    assert len(ev_service.state["requests"]) == 1
    assert ev_service.state["requests"][request_id]["status"] == "cancelled"


def test_new_arrival_expires_overdue_request_before_linking(ev_service):
    """A new connection after an outage gets a new default request instead of linking the expired one."""
    single_car(ev_service)
    old_request = str(uuid4())
    ev_service.save_request(
        {
            "request_id": old_request,
            "vehicle_id": CAR,
            "departure": (NOW + timedelta(seconds=5)).isoformat(),
        },
        NOW,
        "Europe/Amsterdam",
    )
    later = NOW + timedelta(minutes=1)
    ev_service.receive(observations(ev_service, later), later, "Europe/Amsterdam")
    active = ev_service.document(later, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    assert active is not None
    assert active["id"] != old_request
    assert ev_service.state["requests"][old_request]["status"] == "expired"
    assert ev_service.work(later, "Europe/Amsterdam")[0].required_kwh == 18


def test_completed_manual_request_survives_default_changes_on_same_connection(ev_service):
    """A finished manual target does not silently become a new request after editing next-arrival defaults."""
    single_car(ev_service)
    request_id = str(uuid4())
    ev_service.save_request(
        {"request_id": request_id, "vehicle_id": CAR, "target_soc": 50}, NOW, "Europe/Amsterdam"
    )
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    assert ev_service.state["requests"][request_id]["status"] == "completed"
    configuration = settings()
    configuration["points"][0]["vehicle_ids"] = [CAR]
    configuration["vehicles"][0]["target_soc"] = 85
    ev_service.configure(configuration, catalogue(), NOW)
    later = NOW + timedelta(seconds=5)
    ev_service.receive(observations(ev_service, later), later, "Europe/Amsterdam")
    assert ev_service.work(later, "Europe/Amsterdam") == ()
    assert len(ev_service.state["requests"]) == 1


def test_new_physical_connection_can_create_request_after_cancellation(ev_service):
    """Cancellation guards belong to the old nonce and cannot suppress a later actual arrival."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    request = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    ev_service.cancel_request(request["id"], request["version"], NOW, CAR)
    later = NOW + timedelta(seconds=5)
    ev_service.receive(
        observations(ev_service, later, session=str(uuid4())), later, "Europe/Amsterdam"
    )
    new_request = ev_service.document(later, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    assert new_request["id"] != request["id"]
    assert ev_service.work(later, "Europe/Amsterdam")[0].required_kwh == 18


def test_explicit_new_request_replaces_session_terminal_guard(ev_service):
    """A deliberate replacement remains possible, and its own completion is shown for this connection."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    old_request = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    ev_service.cancel_request(old_request["id"], old_request["version"], NOW, CAR)
    replacement_id = str(uuid4())
    ev_service.save_request(
        {"request_id": replacement_id, "vehicle_id": CAR, "target_soc": 50}, NOW, "Europe/Amsterdam"
    )
    later = NOW + timedelta(seconds=5)
    ev_service.receive(observations(ev_service, later), later, "Europe/Amsterdam")
    assert ev_service.state["requests"][replacement_id]["status"] == "completed"
    point = ev_service.document(later, "Europe/Amsterdam")["points"][0]
    assert point["status"] == "request_finished"
    assert point["session_request_status"] == "completed"


@pytest.mark.parametrize("energy, offset", [(None, 0), (100, 6)])
def test_invalid_energy_timestamp_cannot_publish_session_or_soc(ev_service, energy, offset):
    """An unbound or future meter timestamp is rejected before any arrival or telemetry commit."""
    single_car(ev_service)
    body = observations(ev_service)
    body["points"][0].update(
        energy_kwh=energy, energy_observed_at=(NOW + timedelta(seconds=offset)).isoformat()
    )
    with pytest.raises(ValueError, match="Invalid EV meter provenance"):
        ev_service.receive(body, NOW, "Europe/Amsterdam")
    assert ev_service.report is None
    assert ev_service.state["requests"] == {}
    assert ev_service.vehicle_readings(NOW) == {}


@pytest.mark.parametrize("known_connection", [False, True])
def test_timely_completion_report_received_after_departure_remains_completed(
    ev_service, known_connection
):
    """Capture at t=55 proves the t=60 target even when received at t=65, without a replacement request."""
    single_car(ev_service)
    request_id = str(uuid4())
    ev_service.save_request(
        {
            "request_id": request_id,
            "vehicle_id": CAR,
            "departure": (NOW + timedelta(seconds=60)).isoformat(),
        },
        NOW,
        "Europe/Amsterdam",
    )
    if known_connection:
        ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    captured = NOW + timedelta(seconds=55)
    received = NOW + timedelta(seconds=65)
    ev_service.receive(observations(ev_service, captured, soc=80), received, "Europe/Amsterdam")
    assert ev_service.state["requests"][request_id]["status"] == "completed"
    assert len(ev_service.state["requests"]) == 1
    assert ev_service.state["active_requests"] == {}
    later = NOW + timedelta(seconds=70)
    ev_service.receive(observations(ev_service, later, soc=None), later, "Europe/Amsterdam")
    assert len(ev_service.state["requests"]) == 1
    assert ev_service.work(later, "Europe/Amsterdam") == ()


def test_late_delivered_meter_completion_credits_energy_once(ev_service):
    """A timely one-hour 2.3-AC-kWh interval completes a 51% request and adds 1.84 battery kWh once."""
    single_car(ev_service)
    request_id = str(uuid4())
    ev_service.save_request(
        {
            "request_id": request_id,
            "vehicle_id": CAR,
            "target_soc": 51,
            "departure": (NOW + timedelta(seconds=3605)).isoformat(),
        },
        NOW,
        "Europe/Amsterdam",
    )
    first = observations(ev_service)
    first["points"][0]["energy_kwh"] = 100
    ev_service.receive(first, NOW, "Europe/Amsterdam")
    captured = NOW + timedelta(hours=1)
    report = observations(ev_service, captured, soc=None)
    report["points"][0].update(energy_kwh=102.3, energy_observed_at=captured.isoformat())
    ev_service.receive(report, NOW + timedelta(seconds=3610), "Europe/Amsterdam")
    assert ev_service.state["requests"][request_id]["status"] == "completed"
    assert (
        ev_service.state["requests"][request_id]["completion_soc_basis"] == "estimated_from_meter"
    )
    assert ev_service.vehicle_readings(captured)[CAR]["stored_kwh"] == pytest.approx(1.84)
    later = NOW + timedelta(seconds=3620)
    report["at"] = later.isoformat()
    ev_service.receive(report, later, "Europe/Amsterdam")
    assert len(ev_service.state["requests"]) == 1
    assert ev_service.vehicle_readings(later)[CAR]["stored_kwh"] == pytest.approx(1.84)
