"""Strategy archive failures, manual settings validation and off-loop accepted-plan reads."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.optimization.domain import Strategy
from open_energy_planner.optimization.strategy_api import register


@pytest.mark.parametrize(
    "fault", [None, OSError, ValueError, KeyError, TypeError, "corrupt", "revoked"]
)
def test_strategy_read_offloads_archive_and_reports_explicit_unavailability(fault):
    """Read accepted strategy outside the event loop. Corrupt/unreadable archives return 503 and revoked plans stay inactive."""

    async def scenario():
        """Exercise actual HTTP handlers with an observable archive boundary. Persistence contracts are tested separately."""

        def save(_strategy):
            """Unchanged intent must not write to a database in this HTTP-boundary test."""
            raise AssertionError("Unchanged strategy must not be saved")

        store = SimpleNamespace(current=Strategy.COST, issue=None, save=save)
        loop_thread = threading.get_ident()
        threads = []
        plan = {
            "strategy": "minimize cost",
            "config_revision": "same",
            "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }

        def get_plan(_identity):
            """Record native thread ownership and simulate only the archive failure under test."""
            threads.append(threading.get_ident())
            if isinstance(fault, type):
                raise fault("archive fixture")
            return {**plan, "valid_until": "corrupt"} if fault == "corrupt" else plan

        state = {
            "plan": None if fault == "revoked" else plan,
            "acknowledged_plan": "accepted",
            "enabled": True,
        }

        async def persist(save, candidate, **_kwargs):
            """Commit intent without an unrelated planner in this archive-specific test."""
            await asyncio.to_thread(save, candidate)

        app = web.Application()
        register(
            app,
            store,
            SimpleNamespace(state=state, get_plan=get_plan),
            object(),
            asyncio.Lock(),
            persist,
        )
        async with TestClient(TestServer(app)) as client:
            for method, body in (
                ("GET", None),
                ("PUT", {"strategy": "minimize cost", "expected_strategy": "minimize cost"}),
            ):
                response = await client.request(method, "/v1/strategy", json=body)
                assert response.status == (503 if fault not in (None, "revoked") else 200)
                if response.status == 200:
                    assert (await response.json())["active"] == (
                        None if fault == "revoked" else "minimize cost"
                    )
        assert threads and all(identity != loop_thread for identity in threads)

    asyncio.run(scenario())
