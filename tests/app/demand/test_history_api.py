"""Exercise actual authenticated history admission, bounded public errors and event-loop responsiveness."""

import asyncio
import json
from threading import Event, Thread
from urllib.request import Request, urlopen

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.demand import api as demand_api
from open_energy_planner.demand.runtime import DemandRuntime
from tests.app.demand.history_support import history_report
from tests.app.demand.test_demand import channel
from tests.app.demand.test_demand_app import definition, history_body
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.planning.test_live_app import TOKEN
from tests.database.support.archive import list_documents, read_document
from tests.ha_contract.support.metadata import setup_metadata

SENTINEL = "SYNTHETIC_PRIVATE_HISTORY_ERROR"
AUTH = {"Authorization": "Bearer synthetic-history-tests-0123456789abcdef"}


async def configure_history(client):
    """Install the real Energy-owned demand configuration and return its current request."""
    metadata = setup_metadata()
    response = await client.post("/v1/ha/catalogue", headers=AUTH, json=metadata)
    assert response.status == 200
    response = await client.put(
        "/v1/demand",
        headers=AUTH,
        json={
            "terms": metadata["suggestions"]["terms"],
            "annual_kwh": 3500,
            "timezone": "Europe/Amsterdam",
            "exclusions": [],
            "expected_version": None,
        },
    )
    assert response.status == 200
    return (await response.json())["request"]


@pytest.mark.parametrize(
    "error",
    [SENTINEL, {"diagnostic": SENTINEL}, [SENTINEL], "history_unavailable:" + SENTINEL, True, None],
)
def test_history_http_rejects_private_malformed_and_lookalike_errors(tmp_path, error):
    """Unsupported peer diagnostics cannot enter runtime status, responses or persisted demand evidence."""

    async def scenario():
        """Send adversarial error envelopes through the actual authenticated application endpoint."""
        async with TestClient(
            TestServer(
                create_app(
                    tmp_path, "synthetic-history-tests-0123456789abcdef", source="home_assistant"
                )
            )
        ) as client:
            pending = await configure_history(client)
            response = await client.post(
                "/v1/demand/history",
                headers=AUTH,
                json={"request_version": pending["version"], "error": error},
            )
            assert response.status == 400
            assert SENTINEL not in await response.text()
            status = await (await client.get("/v1/demand", headers=AUTH)).json()
            assert status["history_issues"] == {}
            assert status["request"]["version"] == pending["version"]
        for name in list_documents(tmp_path, "demand"):
            assert SENTINEL not in json.dumps(read_document(tmp_path, "demand", name))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error", ["recorder_unavailable", "history_unavailable", "unsupported_statistics"]
)
def test_history_http_retains_canonical_allowed_failure_codes(tmp_path, error):
    """The three admitted peer failures retain their exact public meaning and retry behavior."""

    async def scenario():
        """Exercise actual admission rather than calling the runtime error method directly."""
        async with TestClient(
            TestServer(
                create_app(
                    tmp_path, "synthetic-history-tests-0123456789abcdef", source="home_assistant"
                )
            )
        ) as client:
            pending = await configure_history(client)
            response = await client.post(
                "/v1/demand/history",
                headers=AUTH,
                json={"request_version": pending["version"], "error": error},
            )
            assert response.status == 200
            status = await (await client.get("/v1/demand", headers=AUTH)).json()
            assert status["history_issues"] == {pending["entity_id"]: error}
            assert status["request"] is None

    asyncio.run(scenario())


def test_history_identity_failure_exposes_only_the_fixed_continuity_code(tmp_path):
    """A real changed source envelope maps the internal continuity exception to one fixed public code."""

    async def scenario():
        """Discover source identity, then report another identity through its exact current annual request."""
        async with TestClient(
            TestServer(
                create_app(
                    tmp_path, "synthetic-history-tests-0123456789abcdef", source="home_assistant"
                )
            )
        ) as client:
            pending = await configure_history(client)
            source = channel("sensor.import", [1, 1])
            response = await client.post(
                "/v1/demand/history", headers=AUTH, json=history_report(pending, source)
            )
            assert response.status == 200
            status = await (await client.get("/v1/demand", headers=AUTH)).json()
            pending = status["request"]
            body = history_report(pending, source)
            body["statistics_identity"]["metadata_id"] = 18
            response = await client.post("/v1/demand/history", headers=AUTH, json=body)
            assert response.status == 400
            assert await response.text() == "Energy history source changed"
            status = await (await client.get("/v1/demand", headers=AUTH)).json()
            assert status["history_issues"] == {"sensor.import": "history_continuity_unavailable"}

    asyncio.run(scenario())


@pytest.mark.parametrize("held_operation", ["parse", "receive", "configure"])
def test_history_work_keeps_the_independent_http_heartbeat_live(
    tmp_path, monkeypatch, held_operation
):
    """A held parser, import or configuration restore cannot block an unlocked real HTTP route."""

    async def scenario():
        """An independent observer releases held work after testing the event loop over loopback HTTP."""
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            response = await session.put(url + "/v1/demand", json=definition())
            assert response.status == 200
            body = await history_body(session, url, days=7)
            if held_operation == "configure":
                response = await session.post(url + "/v1/demand/history", json=body)
                assert response.status == 200
                status = await (await session.get(url + "/v1/demand")).json()
                body = definition(
                    annual_kwh=4000, expected_version=status["configuration"]["version"]
                )
            entered, release = Event(), Event()
            observed = {}
            owner, name = (
                (demand_api, "parse_report")
                if held_operation == "parse"
                else (DemandRuntime, held_operation)
            )
            original = getattr(owner, name)

            def held(*args, **kwargs):
                """Keep the actual owned operation waiting until the independent HTTP observation completes."""
                entered.set()
                assert release.wait(10), "Observer did not release held work"
                return original(*args, **kwargs)

            def observe():
                """Read the existing unlocked pairing route while the owner operation is held."""
                try:
                    assert entered.wait(5), "Import did not reach its owned operation"
                    request = Request(
                        url + "/v1/pairing", headers={"Authorization": "Bearer " + TOKEN}
                    )
                    with urlopen(request, timeout=1) as heartbeat:
                        observed["responsive_while_held"] = (
                            heartbeat.status == 200 and not release.is_set()
                        )
                except OSError:
                    observed["responsive_while_held"] = False
                finally:
                    release.set()

            monkeypatch.setattr(owner, name, held)
            observer = Thread(target=observe, daemon=True)
            observer.start()
            try:
                response = await session.request(
                    "PUT" if held_operation == "configure" else "POST",
                    url + "/v1/demand" + ("" if held_operation == "configure" else "/history"),
                    json=body,
                )
                assert response.status == 200, await response.text()
            finally:
                release.set()
                await asyncio.to_thread(observer.join)
            assert observed == {"responsive_while_held": True}

    asyncio.run(scenario())
