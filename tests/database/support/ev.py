"""One real-backend EV service fixture without implicit charging requests."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from open_energy_planner.appliances.ev.service import EVService
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.app.ev.support.devices import catalogue, settings
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[name for name, _url in BACKENDS])
def ev_service(request):
    """Use the shared four-backend matrix while keeping each test's schema isolated."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(catalogue())
    service = EVService(
        SqlDocuments(engine, "planning"), consumers, SimpleNamespace(state={"devices": {}})
    )
    service.configure(settings(), catalogue())
    yield service
    SCHEMA.drop_all(engine)
    engine.dispose()


def reconstruct_requests(documents, archived):
    """Replay a historical global revision from its immutable request versions or complete original document."""
    requests = {}
    while archived is not None:
        if "request_changes" not in archived:
            for request_id, request in archived.get("requests", {}).items():
                requests.setdefault(request_id, request)
            break
        for request_id, version in archived["request_changes"].items():
            requests.setdefault(
                request_id, documents.read("ev-request-version-" + version + ".json")
            )
        previous = archived["previous_revision"]
        archived = documents.read("ev-state-" + previous + ".json") if previous else None
    return requests


def recognition_plan(service):
    """Prepare and solve a real shared-point plan with independent positive and excluded-car signals."""
    import json
    from datetime import timedelta

    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.installation.validation import validate
    from open_energy_planner.optimization.domain import Interval, Status
    from open_energy_planner.planning import InputSnapshot
    from tests.app.ev.support.devices import NOW, observations
    from tests.app.ev.test_ev_solver import solve

    configuration, sources = settings(), catalogue()
    for index, vehicle in enumerate(configuration["vehicles"]):
        for field, domain in (
            ("connected_entity", "binary_sensor"),
            ("charging_entity", "binary_sensor"),
            ("location_entity", "device_tracker"),
        ):
            entity = f"{domain}.car_{index}_{field}"
            vehicle[field] = entity
            sources["entities"].append({"entity_id": entity, "unit": "", "device_class": ""})
    service.configure(configuration, sources, NOW)
    body = observations(service)
    for index, vehicle in enumerate(body["vehicles"]):
        for field in ("connected", "charging", "home"):
            vehicle[field] = {
                "value": index == 0,
                "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
                "reported_at": (NOW - timedelta(seconds=10)).isoformat(),
                "timestamp_kind": "ha_report",
            }
    service.receive(body, NOW, "Europe/Amsterdam")
    service.appliances = ApplianceService(service.documents, service.consumers)
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 2, "kwh": 0}],
    }
    service.consumers.result = {
        "residual_baseline": forecast,
        "devices": [
            {
                "entity_id": "sensor.charger_energy",
                "forecast": forecast,
                "baseline_forecast": forecast,
            }
        ],
    }
    inputs = InstallationProblem((Interval(NOW, 2, 0.3, 0),), (), (), (), 20000, 20000)
    snapshot = service.prepare(
        InputSnapshot(
            "recognition-snapshot", "config", NOW, "measurement", "forecast", "prices", inputs
        ),
        None,
        NOW,
        "Europe/Amsterdam",
    )
    solved = solve(snapshot.problem)
    return snapshot, json.loads(
        json.dumps(
            plan_document(
                snapshot,
                SimpleNamespace(plan=solved, status=Status.OPTIMAL),
                validate(snapshot.problem, solved),
                NOW,
            )
        )
    )


def expected_recognition_inputs():
    """Specify both allowed cars' original source bindings and boolean evidence independently."""
    from tests.app.ev.support.devices import CAR, OTHER_CAR

    return [
        {
            "vehicle_id": vehicle_id,
            **{
                field: {
                    "source_entity": f"{domain}.car_{index}_{source_field}",
                    "observation": {
                        "value": value,
                        "observed_at": "2026-10-26T11:59:30+00:00",
                        "reported_at": "2026-10-26T11:59:50+00:00",
                        "timestamp_kind": "ha_report",
                    },
                }
                for field, domain, source_field in (
                    ("connected", "binary_sensor", "connected_entity"),
                    ("charging", "binary_sensor", "charging_entity"),
                    ("home", "device_tracker", "location_entity"),
                )
            },
        }
        for index, vehicle_id, value in ((0, CAR, True), (1, OTHER_CAR, False))
    ]
