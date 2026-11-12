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


@pytest.mark.parametrize(
    "mode",
    [
        "disconnected",
        "unknown_identity",
        "missing_soc",
        "at_target",
        "measurement_only",
        "scheduled",
    ],
)
def test_charger_history_is_never_a_future_ev_request(service, mode):
    """An independent 3-kWh charger expectation disappears for every eligibility state."""
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot

    service.appliances = ApplianceService(service.documents, service.consumers)
    if mode == "measurement_only":
        configuration = settings()
        configuration["points"][0]["control"] = "read_only"
        service.configure(configuration, catalogue())
    service.receive(
        observations(
            service,
            connected=mode != "disconnected",
            soc=None if mode == "missing_soc" else 80 if mode == "at_target" else 50,
        ),
        NOW,
    )
    if mode not in ("unknown_identity", "disconnected"):
        service.assign(POINT, CAR, SESSION, NOW)

    def forecast(kwh):
        """Use independent residual and historical charger energy over a two-hour horizon."""
        return {
            "issued_at": NOW.isoformat(),
            "steps": [{"start": NOW.isoformat(), "duration_h": 2, "kwh": kwh}],
        }

    service.consumers.result = {
        "residual_baseline": forecast(1),
        "devices": [
            {
                "entity_id": "sensor.charger_energy",
                "forecast": forecast(3),
                "baseline_forecast": forecast(3),
            }
        ],
    }
    inputs = InstallationProblem((Interval(NOW, 2, 0.3, 0, load_kwh=4),), (), (), (), 20000, 20000)
    original = InputSnapshot("snapshot", "config", NOW, "measurement", "forecast", "prices", inputs)
    prepared = service.prepare(original, None, NOW, "Europe/Amsterdam")
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(1)
    charger = next(
        row for row in prepared.load_components if row.entity_id == "sensor.charger_energy"
    )
    assert sum(step.kwh for step in charger.forecast_steps) == 3
    assert sum(step.kwh for step in charger.steps) == 0
    assert original.problem.intervals[0].load_kwh == 4
    assert service.issue is None
    if mode == "scheduled":
        assert prepared.problem.ev_work[0].required_kwh == 18
    else:
        assert sum(work.required_kwh for work in prepared.problem.ev_work) == 0


@pytest.mark.parametrize("expected_power, actual_power", [(0, 2300), (2300, 0), (2300, 1500)])
def test_sustained_charging_deviation_requests_one_round_and_explains_recovery(
    service, expected_power, actual_power
):
    """A masked grid deviation still requests one replan after 60 seconds, without retry loops."""
    plan = {
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "steps": [
            {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "devices": {POINT: {"power_w": expected_power}},
            }
        ],
    }

    def measured(seconds, power):
        """Keep captures current even when SOC stays unchanged."""
        now = NOW + timedelta(seconds=seconds)
        body = observations(service, now)
        body["points"][0]["power_w"] = power
        service.receive(body, now)
        return service.execution_deviated(plan, now)

    assert not measured(0, actual_power)
    service.accept_execution_deviations(NOW)
    assert not measured(59, actual_power)
    assert measured(60, actual_power)
    service.accept_execution_deviations(NOW + timedelta(seconds=60))
    for seconds in (120, 180, 240):
        assert not measured(seconds, actual_power)
    assert not measured(300, actual_power)
    issue = service.document(NOW + timedelta(seconds=300), "Europe/Amsterdam")["points"][0][
        "execution_issue"
    ]
    assert "Check the vehicle charge limit" in issue
    assert not measured(310, expected_power)
    assert (
        service.document(NOW + timedelta(seconds=310), "Europe/Amsterdam")["points"][0][
            "execution_issue"
        ]
        is None
    )
    assert not measured(320, actual_power)
    assert measured(380, actual_power)


@pytest.mark.parametrize("power", [None, 0, 499])
def test_missing_power_and_small_noise_do_not_create_unplanned_charging(service, power):
    """No command means zero planned charging, while missing measurements remain unknown."""
    plan = {
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "steps": [
            {"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(), "devices": {}}
        ],
    }
    for seconds in (0, 60, 300):
        now = NOW + timedelta(seconds=seconds)
        body = observations(service, now)
        body["points"][0]["power_w"] = power
        service.receive(body, now)
        assert not service.execution_deviated(plan, now)


def test_ev_power_deviation_obeys_five_minute_throttle_and_does_not_repeat(service):
    """A 2.3-kW unexpected charge hidden at the grid causes one early round, then normal quarters."""
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.live_inputs import LiveInputs
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot

    inputs = InstallationProblem((Interval(NOW, 1, 0.3, 0, load_kwh=0),), (), (), (), 20000, 20000)
    snapshot = InputSnapshot("snapshot", "config", NOW, "measurement", "forecast", "prices", inputs)
    document = {
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "steps": [
            {"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(), "devices": {}}
        ],
        "expected_flows": [{"batteries": [], "grid_import": 0, "grid_export": 0}],
    }
    submitted = []
    clock = [NOW]
    planner = SimpleNamespace(published=(snapshot, document), submit=submitted.append)
    live = LiveInputs(planner, None, clock=lambda: clock[0], ev=service)
    live.assembler = SimpleNamespace(expiry=lambda *_arguments: None)
    live.report = SimpleNamespace(readings=())
    future = SimpleNamespace(
        config_revision="config",
        forecast_version="forecast",
        price_version="prices",
        valid_until=NOW + timedelta(hours=1),
    )
    for seconds in sorted({*range(0, 901, 30), 59, 299, 899}):
        clock[0] = NOW + timedelta(seconds=seconds)
        body = observations(service, clock[0])
        body["points"][0]["power_w"] = 2300
        service.receive(body, clock[0])
        live._install((clock[0], future, snapshot), force=False)
        assert len(submitted) == (1 if seconds < 300 else 2 if seconds < 900 else 3)


@pytest.mark.parametrize(
    "reset", ["missing_power", "capture_gap", "session", "setpoint", "expired_plan"]
)
def test_power_confirmation_never_spans_unknown_data_or_a_different_command(service, reset):
    """A fresh 60-second episode is required after loss of evidence or a changed physical context."""
    plan = {
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "steps": [
            {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "devices": {POINT: {"power_w": 2300}},
            }
        ],
    }
    service.receive(observations(service), NOW)
    assert not service.execution_deviated(plan, NOW)
    if reset == "missing_power":
        midway = NOW + timedelta(seconds=30)
        body = observations(service, midway)
        body["points"][0]["power_w"] = None
        service.receive(body, midway)
        assert not service.execution_deviated(plan, midway)
    later = NOW + timedelta(seconds=120 if reset == "capture_gap" else 60)
    body = observations(service, later)
    if reset == "session":
        body["points"][0]["session_id"] = str(uuid4())
    elif reset == "setpoint":
        plan["steps"][0]["devices"][POINT]["power_w"] = 3000
    elif reset == "expired_plan":
        plan["valid_until"] = later.isoformat()
    service.receive(body, later)
    assert not service.execution_deviated(plan, later)
    if reset != "expired_plan":
        latest = later + timedelta(seconds=60)
        body["at"] = latest.isoformat()
        service.receive(body, latest)
        assert service.execution_deviated(plan, latest)


def test_missing_charger_separation_blocks_live_planning_instead_of_using_history(service):
    """The original household estimate stays archived but cannot publish as a plan-only forecast."""
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.home_assistant.observations import InputsUnavailable
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.live_inputs import LiveInputs
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot

    service.appliances = ApplianceService(service.documents, service.consumers)
    inputs = InstallationProblem((Interval(NOW, 1, 0.3, 0, load_kwh=4),), (), (), (), 20000, 20000)
    original = InputSnapshot("snapshot", "config", NOW, "measurement", "forecast", "prices", inputs)
    live = LiveInputs(None, None, ev=service)
    live.prepare_snapshot = lambda snapshot, now: service.prepare(
        snapshot, None, now, "Europe/Amsterdam"
    )
    unavailable = live._prepare((NOW, None, original))
    assert isinstance(unavailable, InputsUnavailable)
    assert str(unavailable) == "ev_forecast_boundary_unavailable"
    assert original.problem.intervals[0].load_kwh == 4


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


@pytest.mark.parametrize("vehicle_id, maximum_w", [(CAR, 11000), (OTHER_CAR, 3680)])
def test_fixed_point_uses_each_assigned_cars_rate_without_minimum_mismatch(
    service, vehicle_id, maximum_w
):
    """One 11-kW on/off point admits both 3x16-A and 1x16-A cars at their own fixed AC rate."""
    data = settings()
    data["vehicles"][1]["maximum_w"] = 3680
    data["points"][0].update(control="on_off", minimum_w=11000, maximum_w=11000)
    service.configure(data, catalogue())
    service.receive(observations(service), NOW)
    service.assign(POINT, vehicle_id, SESSION, NOW)
    (request,) = service.work(NOW, "Europe/Amsterdam")
    assert request.maximum_w == maximum_w
    assert request.minimum_w == maximum_w
    assert request.control == "on_off"


@pytest.mark.parametrize("power_source", [None, "sensor.replacement_power"])
def test_changed_energy_power_link_withdraws_work_until_settings_are_confirmed(
    service, power_source
):
    """Removing or replacing Energy's power link cannot continue scheduling against the old measurement."""
    service.receive(observations(service), NOW)
    service.assign(POINT, CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam")
    changed = catalogue()
    changed["suggestions"]["consumers"][0].pop("power_entity")
    if power_source:
        changed["suggestions"]["consumers"][0]["power_entity"] = power_source
        changed["entities"].append({**changed["entities"][3], "entity_id": power_source})
    service.consumers.sync(changed)
    assert service.work(NOW, "Europe/Amsterdam") == ()
    assert (
        service.document(NOW, "Europe/Amsterdam")["points"][0]["status"]
        == "energy_source_unavailable"
    )


def test_variable_point_still_rejects_a_vehicle_below_its_minimum(service):
    """A real variable-power lower bound remains mandatory and cannot admit a 3.68-kW car at 11 kW."""
    data = settings()
    data["vehicles"][1]["maximum_w"] = 3680
    data["points"][0].update(control="variable", minimum_w=11000, maximum_w=11000)
    service.configure(data, catalogue())
    service.receive(observations(service), NOW)
    service.assign(POINT, OTHER_CAR, SESSION, NOW)
    assert service.work(NOW, "Europe/Amsterdam") == ()


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
