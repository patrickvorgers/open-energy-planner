"""Capture recovery cannot erase a financial failure or leave an unexplained action flag."""

import asyncio
import json
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from open_energy_planner.demand.calculation.model import EnergyTerm
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.savings import api as savings_api
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import END, START, contract, frame, problem


def settlement_service(repository, location):
    """Use real capture storage and daily processing with a same-day original activation."""
    document = {
        "plan_id": "same-day-original",
        "generated_at": START.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "steps": [],
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(problem()), default=lambda value: value.isoformat())
            )
        },
    }
    repository.state["plan"] = document
    repository.save()
    with repository.engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(START), plan_id=document["plan_id"])
        )
    app = web.Application()
    capture, runtime, _bonus = savings_api.register(
        app,
        repository,
        SimpleNamespace(current=contract()),
        SimpleNamespace(current=None),
        SimpleNamespace(current=SimpleNamespace(batteries=())),
        SimpleNamespace(configuration=SimpleNamespace(sources=())),
        SimpleNamespace(
            configuration=SimpleNamespace(
                terms=(EnergyTerm("sensor.import", 1), EnergyTerm("sensor.export", -1))
            )
        ),
        asyncio.Lock(),
        SimpleNamespace(value=None),
        lambda: location[0],
        mutation_lock=asyncio.Lock(),
    )
    return app, capture, runtime


def captured_body(pending):
    """One measured quarter has no PV or battery channels in the configured capture bindings."""
    measured = replace(
        frame(), channels={"sensor.import": 1, "sensor.export": 0}, battery_start={}, battery_end={}
    )
    return {
        "request_version": pending["version"],
        "frames": [measured.record()],
        "imports": ["sensor.import"],
        "exports": ["sensor.export"],
    }


def test_startup_location_recovery_after_capture_needs_no_completed_day(tmp_path, monkeypatch):
    """Real startup, capture and same-day recovery clear both prerequisite fields without calculating a day."""

    class FixedClock(datetime):
        """Keep the worker and original activation on the same independently specified calendar day."""

        @classmethod
        def now(cls, timezone=None):
            """Leave enough completed quarters for a real bounded capture request."""
            instant = START + timedelta(hours=1)
            return instant.astimezone(timezone) if timezone else instant.replace(tzinfo=None)

    monkeypatch.setattr(savings_api, "datetime", FixedClock)
    monkeypatch.setattr(savings_api, "SAVINGS_WORKER_SECONDS", 3600)

    async def scenario(repository):
        """Observe the actual startup worker before accepting history and running its next daily step."""
        location = [None]
        app, capture, runtime = settlement_service(repository, location)
        async with TestServer(app) as server:
            async with asyncio.timeout(3):
                while not runtime.attention:
                    await asyncio.sleep(0.01)
            expected = "Installation location is required for settlement. Configure the location under Installation"
            assert runtime.issue == expected
            location[0] = SiteLocation(52, 6, "UTC")
            pending = await capture()
            async with (
                ClientSession() as client,
                client.post(
                    server.make_url("/v1/settlement"), json=captured_body(pending)
                ) as response,
            ):
                assert response.status == 200, await response.text()
            assert runtime.issue == expected
            assert runtime.attention is True
            assert (
                DailySavings(runtime).process(
                    contract(), None, FixedClock.now(START.tzinfo), location[0]
                )
                is None
            )
            assert runtime.issue is None
            assert runtime.attention is False
            assert len(runtime.store.frames()) == 1
            assert not runtime.documents.exists("daily-progress.json")

    with closing(SqlPlanRepository(tmp_path)) as repository:
        asyncio.run(scenario(repository))


@pytest.mark.parametrize("attention", [False, True])
def test_capture_preserves_an_unresolved_financial_status(tmp_path, attention):
    """Accepted raw measurements cannot claim recovery of a separate financial failure or pending result."""

    async def scenario(repository):
        """Use the real request and atomic receive without starting an unrelated financial worker."""
        location = [SiteLocation(52, 6, "UTC")]
        _app, _capture, runtime = settlement_service(repository, location)
        pending = runtime.request(
            START + timedelta(hours=1),
            SimpleNamespace(batteries=()),
            SimpleNamespace(sources=()),
            SimpleNamespace(
                terms=(EnergyTerm("sensor.import", 1), EnergyTerm("sensor.export", -1))
            ),
            (),
            location[0],
        )
        runtime.issue, runtime.attention = (
            "Existing financial evidence needs investigation",
            attention,
        )
        runtime.receive(
            pending, captured_body(pending), END + timedelta(minutes=1), (), location[0]
        )
        assert runtime.issue == "Existing financial evidence needs investigation"
        assert runtime.attention is attention
        assert len(runtime.store.frames()) == 1

    with closing(SqlPlanRepository(tmp_path)) as repository:
        asyncio.run(scenario(repository))


def test_capture_request_recovery_clears_only_its_own_failure(tmp_path, monkeypatch):
    """A recovered archive request clears its warning even when no full day or new capture is due."""

    async def scenario(repository):
        """Fail and recover the actual asynchronous capture-request boundary."""
        _app, capture, runtime = settlement_service(repository, [SiteLocation(52, 6, "UTC")])

        def unavailable_request(*_arguments):
            """Model an unexpected metadata-read failure without exposing a private driver message."""
            raise OSError("Synthetic archive unavailable")

        monkeypatch.setattr(runtime, "request", unavailable_request)
        assert await capture() is None
        assert runtime.issue == "Settlement capture unavailable. Inspect application logs"
        assert runtime.attention is True
        monkeypatch.setattr(runtime, "request", lambda *_arguments: None)
        assert await capture() is None
        assert runtime.issue is None
        assert runtime.attention is False
        runtime.issue, runtime.attention = "An unresolved financial failure", True
        assert await capture() is None
        assert runtime.issue == "An unresolved financial failure"
        assert runtime.attention is True

    with closing(SqlPlanRepository(tmp_path)) as repository:
        asyncio.run(scenario(repository))
