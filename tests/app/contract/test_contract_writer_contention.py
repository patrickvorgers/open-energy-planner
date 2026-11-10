"""Check the user's save path with synthetic HTTP, storage and independent source fixtures."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from sqlalchemy import event

from open_energy_planner.storage.adapters.activities import ActivityStore
from open_energy_planner.storage.adapters.lifecycle import sqlalchemy as archive
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_app import running


def test_paid_window_toggle_over_http_during_activity_write(tmp_path, monkeypatch):
    """Persist and reload the exact toggle while another writer holds SQL; reject a stale second save."""
    monkeypatch.setattr(archive, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(archive, "SQLITE_BUSY_TIMEOUT_MS", 100)
    original_factory = archive.archive_engine
    engines = []

    def capture_engine(*args, **kwargs):
        """Observe composition's actual shared engine without replacing its database semantics."""
        engine = original_factory(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(archive, "archive_engine", capture_engine)

    async def scenario():
        """Serve real authenticated requests while a synthetic Activity write owns the shared SQL lock."""
        async with running(tmp_path) as (session, url):
            engine = engines[-1]
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "synthetic-bonus",
                    "effective_from": "2026-01-01T00:00:00+00:00",
                    "rate_formula": "0.04",
                    "cap_kwh": 5000,
                    "timezone": "UTC",
                    "prevent_battery_export": False,
                }
            ]
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
                original = await response.json()
            body["expected_version"] = original["version"]
            body["bonuses"][0]["prevent_battery_export"] = True
            started, release = Event(), Event()

            def hold_activity(_connection, _cursor, statement, parameters, _context, _many):
                """Delay only this explicit synthetic Activity write after its INSERT."""
                if (
                    statement.startswith("INSERT INTO oep_activities")
                    and "synthetic-http-writer" in parameters
                ):
                    started.set()
                    assert release.wait(5)

            event.listen(engine, "after_cursor_execute", hold_activity)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    now = datetime.now(UTC)
                    writer = pool.submit(
                        ActivityStore(engine).write,
                        {
                            "id": "synthetic-http-writer",
                            "started_at": now.isoformat(),
                            "owner": "synthetic",
                            "kind": "synthetic",
                            "state": "completed",
                        },
                        now,
                    )
                    assert await asyncio.to_thread(started.wait, 5)
                    timer = asyncio.get_running_loop().call_later(0.35, release.set)
                    try:
                        save = asyncio.create_task(session.put(url + "/v1/contracts", json=body))
                        # A separate read-only route must respond while the contract write waits.
                        async with session.get(url + "/v1/version", timeout=0.2) as response:
                            assert response.status == 200
                        async with await save as response:
                            assert response.status == 200
                            saved = await response.json()
                    finally:
                        release.set()
                        timer.cancel()
                        writer.result(timeout=5)
            finally:
                event.remove(engine, "after_cursor_execute", hold_activity)
            assert saved["bonuses"][0]["prevent_battery_export"] is True
            assert saved["version"] != original["version"]
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 409
            async with session.get(url + "/v1/contracts") as response:
                assert (await response.json())["version"] == saved["version"]
        async with (
            running(tmp_path) as (session, url),
            session.get(url + "/v1/contracts") as response,
        ):
            reopened = await response.json()
            assert reopened["bonuses"][0]["prevent_battery_export"] is True
            assert reopened["version"] == saved["version"]

    asyncio.run(scenario())
