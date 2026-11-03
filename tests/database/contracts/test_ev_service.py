"""EV authority, native intent semantics and durable state on all supported database backends."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from open_energy_planner.appliances.ev.service import EVService, departure
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.app.ev.support.devices import (
    CAR,
    NOW,
    OTHER_CAR,
    OTHER_POINT,
    POINT,
    SESSION,
    catalogue,
    observations,
    settings,
)
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def service(request, tmp_path):
    """Use a dedicated backend schema, exercising JSON transactions without depending on local Docker."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(catalogue())
    app = EVService(
        SqlDocuments(engine, "planning"), consumers, SimpleNamespace(state={"devices": {}})
    )
    app.configure(settings(), catalogue())
    yield app
    SCHEMA.drop_all(engine)
    engine.dispose()


def test_unidentified_car_is_never_planned_even_if_only_one_is_allowed(service):
    """A connected sensor proves presence, not identity. No default car is inferred."""
    service.receive(observations(service), NOW)
    assert service.work(NOW, "Europe/Amsterdam") == ()
    assert service.document(NOW, "Europe/Amsterdam")["points"][0]["status"] == "vehicle_unknown"


def test_explicit_car_uses_its_own_soc_capacity_power_and_local_departure(service):
    """Car B needs (70-20)% of 40 kWh = 20 kWh, capped at 7.4 kW and departing at 07:00 UTC."""
    service.receive(observations(service), NOW)
    service.assign(POINT, OTHER_CAR, SESSION, NOW)
    (work,) = service.work(NOW, "Europe/Amsterdam")
    assert work.required_kwh == 20
    assert work.maximum_w == 7400
    assert work.ready_at == NOW + timedelta(seconds=130)
    assert work.deadline == datetime(2026, 10, 27, 7, tzinfo=UTC)
    assert service.consumers.selected == (
        {"entity_id": "sensor.charger_energy", "weather_sensitive": False},
    )


def test_reconnect_and_restart_never_reuse_a_previous_vehicle_assignment(service):
    """A new connection nonce or missing post-restart capture withdraws old car authorization."""
    service.receive(observations(service), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam")
    restored = EVService(service.documents, service.consumers, service.appliances)
    assert restored.work(NOW, "Europe/Amsterdam") == ()
    later = NOW + timedelta(seconds=5)
    service.receive(observations(service, later, session=str(uuid4())), later)
    assert service.work(later, "Europe/Amsterdam") == ()
    with pytest.raises(ValueError, match="fresh connected"):
        service.assign(POINT, CAR, SESSION, later)


def test_soc_updates_are_soft_but_missing_readings_and_identity_changes_are_hard(service):
    """Normal SoC progress changes remaining kWh without starving publication. Missing evidence revokes eligibility."""
    service.receive(observations(service), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    revision = service.hard_revision(NOW, "Europe/Amsterdam")
    later = NOW + timedelta(seconds=5)
    service.receive(observations(service, later, soc=60), later)
    assert service.hard_revision(later, "Europe/Amsterdam") == revision
    assert service.work(later, "Europe/Amsterdam")[0].required_kwh == 12
    later += timedelta(seconds=5)
    service.receive(observations(service, later, soc=None), later)
    assert service.hard_revision(later, "Europe/Amsterdam") != revision
    assert service.work(later, "Europe/Amsterdam") == ()


def test_measurement_only_and_expired_reports_never_authorize_charging(service):
    """Read-only capability remains measured load, while a transport outage withdraws controllable EV eligibility."""
    data = settings()
    data["points"][0]["control"] = "read_only"
    service.configure(data, catalogue())
    service.receive(observations(service), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam") == ()
    assert service.document(NOW, "Europe/Amsterdam")["points"][0]["status"] == "measurement_only"
    assert service.live(NOW + timedelta(seconds=120)) == []


def test_target_and_one_off_departure_survive_backend_restart(service):
    """Native dashboard intent is durable. An expired override returns automatically to the daily HA clock."""
    service.intent(CAR, "target_soc", 85, NOW, "Europe/Amsterdam")
    override = NOW + timedelta(hours=2)
    service.intent(CAR, "departure_override", override.isoformat(), NOW, "Europe/Amsterdam")
    restored = EVService(service.documents, service.consumers, service.appliances)
    car = restored.state["vehicles"][0]
    assert car["target_soc"] == 85
    assert departure(car, NOW, "Europe/Amsterdam") == override
    assert departure(car, override, "Europe/Amsterdam") == datetime(2026, 10, 27, 6, tzinfo=UTC)


@pytest.mark.parametrize("change", ["unknown_meter", "duplicate_soc", "overlap", "unbounded_power"])
def test_invalid_configuration_leaves_the_committed_database_unchanged(service, change):
    """Invalid discovery membership or physical limits cannot replace the current EV settings."""
    before = deepcopy(service.state)
    invalid = settings()
    if change == "unknown_meter":
        invalid["points"][0]["energy_entity"] = "sensor.not_in_energy"
    elif change == "duplicate_soc":
        invalid["vehicles"][1]["soc_entity"] = "sensor.car_soc"
    elif change == "overlap":
        invalid["points"].append({**invalid["points"][0], "id": OTHER_POINT})
    else:
        invalid["vehicles"][0]["maximum_w"] = float("inf")
    with pytest.raises(ValueError):
        service.configure(invalid, catalogue())
    assert service.state == before
    assert EVService(service.documents, service.consumers, service.appliances).state == before


def test_small_ha_clock_skew_is_fresh_but_never_extends_maximum_report_age(service):
    """A HA clock two seconds ahead stays usable. Six seconds ahead and age 120 seconds are rejected/unavailable."""
    ahead = NOW + timedelta(seconds=2)
    service.receive(observations(service, ahead), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam")
    assert service.vehicle_readings(NOW)
    assert service.live(ahead + timedelta(seconds=119))
    assert service.live(ahead + timedelta(seconds=120)) == []
    assert service.vehicle_readings(ahead + timedelta(seconds=120)) == {}
    before = deepcopy(service.report)
    with pytest.raises(ValueError, match="future"):
        service.receive(observations(service, NOW + timedelta(seconds=6)), NOW)
    assert service.report == before


def test_removing_energy_consumer_withdraws_ev_work_without_deleting_history(service):
    """A removed Energy meter revokes current charging eligibility and changes the hard revision, retaining saved intent."""
    service.receive(observations(service), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    revision = service.hard_revision(NOW, "Europe/Amsterdam")
    before = deepcopy(service.state)
    service.consumers.sync({"entities": [], "suggestions": {"consumers": []}})
    assert service.work(NOW, "Europe/Amsterdam") == ()
    assert service.hard_revision(NOW, "Europe/Amsterdam") != revision
    assert (
        service.document(NOW, "Europe/Amsterdam")["points"][0]["status"]
        == "energy_source_unavailable"
    )
    assert service.state == before


def test_expired_readings_are_not_exposed_as_current_soc(service):
    """After two minutes without a new report, neither connection readiness nor measured SoC remains current."""
    service.receive(observations(service), NOW)
    data = service.document(NOW + timedelta(minutes=2), "Europe/Amsterdam")
    assert all(vehicle["actual"] is None for vehicle in data["vehicles"])
    assert data["points"][0]["status"] == "telemetry_unavailable"


def test_unchanged_soc_is_usable_only_while_integration_reports_are_current(service):
    """An eight-hour-old HA SoC supports identified charging, but a two-minute bridge outage still revokes it."""
    body = observations(service)
    old = (NOW - timedelta(hours=8)).isoformat()
    for vehicle in body["vehicles"]:
        vehicle["observed_at"] = vehicle["reported_at"] = old
    service.receive(body, NOW)
    service.assign(POINT, OTHER_CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam")[0].required_kwh == 20
    assert service.vehicle_readings(NOW)[OTHER_CAR]["observed_at"] == old
    assert service.work(NOW + timedelta(minutes=2), "Europe/Amsterdam") == ()


def test_cross_kind_identity_collision_cannot_merge_virtual_car_and_point_devices(service):
    """A vehicle and point sharing an ID would collide in HA. Reject it before committing either record."""
    invalid = settings()
    invalid["points"][0]["id"] = CAR
    with pytest.raises(ValueError, match="distinct identities"):
        service.configure(invalid, catalogue())


def test_current_ev_capture_retains_old_sample_metadata_without_rejecting_available_soc(service):
    """A current capture can carry a two-hour-old unchanged SoC. Preserve its original sample time and value."""
    data = settings()
    data["vehicles"][0]["soc_timestamp_attribute"] = "sampled_at"
    service.configure(data, catalogue())
    report = observations(service)
    report["vehicles"][0].update(
        timestamp_kind="device_sample", observed_at=(NOW - timedelta(hours=2)).isoformat()
    )
    service.receive(report, NOW)
    assert service.report["vehicles"][0]["observed_at"] == (NOW - timedelta(hours=2)).isoformat()
    assert service.vehicle_readings(NOW)
    assert not service.vehicle_readings(NOW + timedelta(minutes=2))


def test_daily_departure_obeys_dst_without_creating_two_fallback_departures():
    """02:30 does not exist in Amsterdam on 29 March 2026. A fallback day's second occurrence is not another departure."""
    car = {"departure_time": "02:30"}
    assert departure(car, datetime(2026, 3, 29, 0, tzinfo=UTC), "Europe/Amsterdam") == datetime(
        2026, 3, 30, 0, 30, tzinfo=UTC
    )
    assert departure(
        car, datetime(2026, 10, 25, 0, 45, tzinfo=UTC), "Europe/Amsterdam"
    ) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_consumer_api_preserves_saved_charging_membership_without_timezone(service):
    """Persisted point ownership stays readable when the current HA catalogue loses timezone and meter discovery."""
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.demand.consumer_api import register

    async def scenario():
        """Read the consumer endpoint without invoking the timezone-dependent EV status document."""
        service.consumers.sync({"entities": [], "suggestions": {"consumers": []}})
        app = web.Application()
        register(
            app,
            SimpleNamespace(consumers=service.consumers, forecast=None),
            asyncio.Lock(),
            service,
            SimpleNamespace(state={"stores": [{"energy_entity": "sensor.boiler_energy"}]}),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/consumers")
            assert response.status == 200
            body = await response.json()
            assert body["charging_meters"] == ["sensor.charger_energy"]
            assert body["thermal_meters"] == ["sensor.boiler_energy"]
            assert body["options"] == []

    asyncio.run(scenario())
