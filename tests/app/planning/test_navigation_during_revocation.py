"""Public navigation remains available while durable command revocation is pending."""

import asyncio
import threading

import pytest

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.planning.test_live_app import (
    FutureSource,
    configure,
    running_app,
    send,
    wait_ready,
)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("save_fails", [False, True])
def test_public_navigation_does_not_wait_for_pending_command_revocation(
    tmp_path, monkeypatch, method, save_fails
):
    """Serve pages and assets during a held revocation while protected state still waits for durability."""

    async def scenario():
        """Hold the real archive writer after missing forecasts revoke a previously published plan."""
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            configured = await configure(session, url)
            await send(session, url, configured["measurement_config"])
            await wait_ready(session, url)
            started, release = threading.Event(), threading.Event()
            original_save = SqlPlanRepository.save

            def hold_revocation(repository):
                """Delay only an actual revocation write, without blocking the server event loop."""
                if repository.pending_invalidation:
                    started.set()
                    assert release.wait(10), "Revocation writer was not released"
                    if save_fails:
                        raise OSError("Synthetic archive outage")
                original_save(repository)

            monkeypatch.setattr(SqlPlanRepository, "save", hold_revocation)
            future.available = False
            invalidation = asyncio.create_task(session.post(url + "/v1/replan"))
            public_reads = []
            protected_read = None
            try:
                assert await asyncio.to_thread(started.wait, 5)
                paths = ("/", "/setup", "/setup/workspace.js", "/setup/style.css", "/setup/session")
                public_reads = [
                    asyncio.create_task(
                        session.request(method, url + path, headers={"Authorization": ""})
                    )
                    for path in paths
                ]
                protected_read = asyncio.create_task(session.get(url + "/v1/snapshot"))
                completed, pending = await asyncio.wait(public_reads, timeout=0.5)
                assert len(completed) == len(paths), "Public navigation waited for command storage"
                assert not pending
                assert not invalidation.done(), "Command revocation returned before persistence"
                assert not protected_read.done(), "Protected state bypassed pending persistence"
                for response in await asyncio.gather(*public_reads):
                    assert response.status == 200
                    await response.read()
            finally:
                release.set()
                await asyncio.gather(
                    invalidation,
                    *public_reads,
                    *([protected_read] if protected_read else []),
                )
                monkeypatch.setattr(SqlPlanRepository, "save", original_save)
            assert (await invalidation).status == (503 if save_fails else 200)
            response = await protected_read
            assert response.status == (503 if save_fails else 200)
            response = await session.get(url + "/v1/snapshot")
            assert response.status == 200
            assert (await response.json())["plan"] is None

    asyncio.run(scenario())
