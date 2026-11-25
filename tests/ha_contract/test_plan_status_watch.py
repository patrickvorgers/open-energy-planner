"""Verify watch authentication and source-revision wakes through the actual TLS gateway."""

import asyncio
import threading

from aiohttp import ClientSession, Fingerprint

from custom_components.open_energy_planner.client import LocalClient
from open_energy_planner.app import create_app
from open_energy_planner.discovery import certificate, connection_app
from open_energy_planner.pairing import PAIRING_KEY
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.planning.test_live_app import FutureSource, configure, running_app, send, wait_ready
from tests.ha_contract.support.connection import PEER, SECRET, TOKEN, server


def test_paired_watch_wakes_for_source_reconfiguration_and_preserves_headers(tmp_path):
    """An authorized peer sees only a new cursor, then obtains current source authority separately."""

    async def scenario():
        """Exercise the app, pinned TLS, release checks, source update and bounded watch."""
        application = create_app(tmp_path, TOKEN, source="home_assistant")
        pairing = application[PAIRING_KEY]
        tls, pin = certificate(tmp_path, pairing.instance_id)
        pairing.fingerprint = pin
        request = pairing.request(PEER)
        await pairing.approve(request["request_id"])
        peer_token = pairing.claim({"request_id": request["request_id"], "secret": SECRET})["token"]

        async with server(application) as upstream:
            gateway = connection_app(pairing, TOKEN, upstream, upstream + "/setup")
            async with server(gateway, tls) as url, ClientSession() as session:
                path = "/v1/ha/plan-status/watch"
                async with session.get(url + path, ssl=Fingerprint(bytes.fromhex(pin))) as reply:
                    assert reply.status == 401
                client = LocalClient(
                    session,
                    url,
                    peer_token,
                    pin,
                    expected_instance_id=pairing.instance_id,
                )
                initial = await client.plan_status()
                assert initial["measurements_required"] is False
                cursor = await client.watch_plan_status(None)
                admin = LocalClient(session, upstream, TOKEN)
                pending = asyncio.create_task(client.watch_plan_status(cursor))
                try:
                    await admin.configure_sources(
                        [
                            {
                                "key": "grid.power",
                                "entity_id": "sensor.synthetic",
                                "quantity": "power",
                            }
                        ],
                        None,
                    )
                    changed = await asyncio.wait_for(pending, timeout=3)
                    assert changed != cursor
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                current = await client.plan_status()
                assert current["measurements_required"] is True
                assert current["config_revision"] != initial["config_revision"]
                async with session.get(
                    url + path,
                    ssl=Fingerprint(bytes.fromhex(pin)),
                    headers={"Authorization": "Bearer " + peer_token},
                ) as reply:
                    assert reply.status == 200
                    assert reply.headers["Cache-Control"] == "no-store"
                    document = await reply.json()
                    assert document.keys() == {"cursor"}
                    assert document["cursor"] != cursor
                    assert len(document["cursor"]) == 32

    asyncio.run(scenario())


def test_revocation_wakes_watch_while_archive_write_is_pending(tmp_path, monkeypatch):
    """Immediately emit revocation even though the durable writer cannot finish yet."""

    async def scenario():
        """Use a real live plan and gate only the storage write that removes its active pointer."""
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            configured = await configure(session, url)
            await send(session, url, configured["measurement_config"])
            await wait_ready(session, url)
            async with session.get(url + "/v1/ha/plan-status/watch") as response:
                cursor = (await response.json())["cursor"]

            save = SqlPlanRepository.save
            entered, release = threading.Event(), threading.Event()

            def hold_revocation(repository):
                """Pause the real revocation write off the app loop until the watch is observed."""
                if repository.pending_invalidation:
                    entered.set()
                    assert release.wait(5)
                return save(repository)

            monkeypatch.setattr(SqlPlanRepository, "save", hold_revocation)
            waiting = asyncio.create_task(
                session.get(url + "/v1/ha/plan-status/watch", params={"cursor": cursor})
            )
            future.available = False
            invalidation = asyncio.create_task(session.post(url + "/v1/replan"))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                async with asyncio.timeout(1):
                    async with await waiting as signal:
                        assert signal.status == 200
                        assert (await signal.json())["cursor"] != cursor
                assert not invalidation.done()
            finally:
                release.set()
                if not waiting.done():
                    waiting.cancel()
                for response in await asyncio.gather(invalidation, waiting, return_exceptions=True):
                    if not isinstance(response, BaseException):
                        response.release()

    asyncio.run(scenario())
