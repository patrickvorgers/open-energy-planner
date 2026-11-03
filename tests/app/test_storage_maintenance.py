"""Exercise maintenance admission, trust, restart and recovery without production databases."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.runtime.maintenance import MAINTENANCE, create_managed_app
from open_energy_planner.storage.adapters.lifecycle.maintenance import (
    JOURNAL_FILE,
    check_candidate,
    read_private,
    recover_source,
    selected_url,
    write_private,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

TOKEN = "maintenance-test-private-token-123456789"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SECRET_URL = "postgresql+psycopg://private-user:password-secret@unavailable.invalid/oep_archive"


def journal(root, *, released=False, phase="copying", target_url=SECRET_URL):
    """Seed one private interrupted operation independently of the controller's transitions."""
    SqlPlanRepository(root).close()
    write_private(
        root,
        JOURNAL_FILE,
        {
            "source_url": None,
            "target_url": target_url,
            "released": released,
            "phase": phase,
            "error": None,
        },
    )


def test_gateway_keeps_normal_auth_and_rejects_forged_ingress(tmp_path):
    """Loopback forwarding must not turn a browser-supplied ingress header into authentication."""

    async def scenario():
        """Exercise real app middleware through the managed public endpoint."""
        app = create_managed_app(
            tmp_path, TOKEN, factory=create_app, source="home_assistant", ingress_peer="192.0.2.1"
        )
        async with TestClient(TestServer(app)) as client:
            spoof = {"X-Ingress-Path": "/api/hassio_ingress/forged"}
            assert (await client.get("/v1/snapshot", headers=spoof)).status == 401
            assert (await client.get("/v1/storage/migration", headers=spoof)).status == 401
            assert (await client.get("/data-storage")).status == 200
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 200
            response = await client.get("/v1/storage/migration", headers=HEADERS)
            assert response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["running"] is True
            response = await client.post(
                "/v1/storage/migration/check", headers=HEADERS, json={"url": None}
            )
            assert response.status == 400
            assert "password" not in await response.text()

    asyncio.run(scenario())


def test_trusted_ingress_still_requires_private_proof_on_internal_socket(tmp_path):
    """Only the validated public proxy can assert ingress identity to the managed runtime."""
    from aiohttp import ClientSession

    async def scenario():
        """Contrast trusted outer ingress with an untrusted direct request to the loopback listener."""
        app = create_managed_app(
            tmp_path, TOKEN, factory=create_app, source="home_assistant", ingress_peer="127.0.0.1"
        )
        ingress = {"X-Ingress-Path": "/api/hassio_ingress/real_fixture"}
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/snapshot")).status == 401
            assert (await client.get("/v1/snapshot", headers=ingress)).status == 200
            assert (await client.get("/v1/storage/migration", headers=ingress)).status == 200
            async with ClientSession() as direct:
                response = await direct.get(
                    app[MAINTENANCE].upstream + "/v1/snapshot", headers=ingress
                )
                assert response.status == 403

    asyncio.run(scenario())


@pytest.mark.parametrize("released", [False, True])
def test_interrupted_migration_is_readable_and_only_unreleased_source_can_restore(
    tmp_path, released
):
    """A process restart retains maintenance UI and blocks all ordinary handlers before recovery."""
    journal(tmp_path, released=released)

    async def scenario():
        """Read durable status, reject unsafe recovery, and resume source only before release."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/storage/migration", headers=HEADERS)
            status = await response.json()
            assert status["phase"] == "interrupted"
            assert status["can_restore"] is not released
            assert "private-user" not in str(status) and "password-secret" not in str(status)
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 503
            from script.runtime.healthcheck import check

            await asyncio.to_thread(check, str(client.make_url("/")).rstrip("/"), TOKEN)
            assert (await client.get("/data-storage")).status == 200
            reopened = await client.get("/")
            assert reopened.status == 200 and reopened.url.path == "/data-storage"
            response = await client.post("/v1/storage/migration/restore", headers=HEADERS, json={})
            assert response.status == (400 if released else 202)
            if not released:
                await app[MAINTENANCE].task
                assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 200
                assert selected_url(tmp_path, SECRET_URL) is None

    asyncio.run(scenario())


def test_start_requires_fresh_preflight_explicit_consent_and_single_owner(tmp_path, monkeypatch):
    """Only confirmed preflight starts maintenance. Its task closes admission and owns the operation."""
    from open_energy_planner.runtime import maintenance

    async def scenario():
        """Hold the transfer to prove status stays available and duplicate actions are rejected."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        held = asyncio.Event()

        async def transfer():
            """Keep the operation alive without opening an external destination."""
            await held.wait()

        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            monkeypatch.setattr(host, "transfer", transfer)
            monkeypatch.setattr(
                maintenance,
                "check_destination",
                lambda *args: {"backend": "PostgreSQL", "contains_oep_data": True},
            )
            checked = await client.post(
                "/v1/storage/migration/check", headers=HEADERS, json={"url": SECRET_URL}
            )
            check_id = (await checked.json())["check_id"]
            for body in ({"check_id": check_id}, {"check_id": "stale", "overwrite": True}):
                assert (
                    await client.post("/v1/storage/migration/start", headers=HEADERS, json=body)
                ).status == 400
            response = await client.post(
                "/v1/storage/migration/start",
                headers=HEADERS,
                json={"check_id": check_id, "overwrite": True},
            )
            assert response.status == 202
            assert read_private(tmp_path, JOURNAL_FILE)["released"] is False
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 503
            assert (await client.get("/v1/storage/migration", headers=HEADERS)).status == 200
            assert (
                await client.post("/v1/storage/migration/restore", headers=HEADERS, json={})
            ).status == 409
            held.set()

    asyncio.run(scenario())


def test_candidate_checks_rollback_probe_and_preserve_source_recovery(tmp_path):
    """Startup validation neither retains its write probe nor modifies durable source selection."""
    from sqlalchemy import select

    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import META

    journal(tmp_path)
    engine = check_candidate(tmp_path, None)
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(META.c.id).where(META.c.id == -1)) is None
        assert read_private(tmp_path, JOURNAL_FILE)["released"] is False
    finally:
        engine.dispose()
    recover_source(tmp_path)
    assert read_private(tmp_path, JOURNAL_FILE)["phase"] == "restored"


def test_release_is_durable_before_runtime_start_and_failed_start_cannot_restore(
    tmp_path, monkeypatch
):
    """Even a startup error after release must not advertise unsafe rollback to old history."""
    from open_energy_planner.runtime import maintenance

    async def scenario():
        """Simulate copy success with SQLite. Inject the failure at the real startup boundary."""
        journal(tmp_path, target_url=None)
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            monkeypatch.setattr(maintenance, "migrate_archive", lambda *args, **kwargs: {})

            async def fail_start(*args):
                """Check crash-safe release intent before any background worker is allowed to run."""
                assert read_private(tmp_path, JOURNAL_FILE)["released"] is True
                raise OSError("secret driver connection failure")

            monkeypatch.setattr(host, "start_runtime", fail_start)
            await host.transfer()
            status = await (await client.get("/v1/storage/migration", headers=HEADERS)).json()
            assert status["released"] is True and status["can_restore"] is False
            assert status["phase"] == "failed" and not status["running"]
            assert "secret" not in str(status)
            with pytest.raises(ValueError, match="unreleased"):
                recover_source(tmp_path)

    asyncio.run(scenario())


def test_sqlite_selection_overrides_stale_external_environment(tmp_path, monkeypatch):
    """Returning to SQLite remains effective after the next process restart despite old container options."""
    write_private(tmp_path, "database-selection.json", {"url": None})
    monkeypatch.setenv("OEP_DATABASE_URL", SECRET_URL)

    async def scenario():
        """Start the actual runtime using explicit SQLite rather than falling back to the environment."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/storage", headers=HEADERS)
            assert response.status == 200
            assert (await response.json())["backend"] == "sqlite"

    asyncio.run(scenario())


def test_unreadable_journal_keeps_recovery_page_but_never_opens_a_database(tmp_path):
    """Corrupt recovery state cannot silently start SQLite or offer a destructive retry."""
    (tmp_path / "database-migration.json").write_text("{broken", encoding="utf-8")

    async def scenario():
        """Read sanitized status through the stable maintenance host while ordinary APIs remain blocked."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/data-storage")).status == 200
            status = await (await client.get("/v1/storage/migration", headers=HEADERS)).json()
            assert status["phase"] == "failed" and status["backend"] == "Unknown"
            assert not status["running"] and not status["can_restore"]
            assert not (tmp_path / "archive.sqlite3").exists()
            assert (
                await client.post(
                    "/v1/storage/migration/retry", headers=HEADERS, json={"overwrite": True}
                )
            ).status == 503

    asyncio.run(scenario())


def test_transfer_waits_for_inflight_writes_and_worker_shutdown(tmp_path, monkeypatch):
    """Copying cannot race an accepted request or any background runtime lifecycle owner."""
    from aiohttp import web, web_protocol

    from open_energy_planner.runtime import maintenance

    original_timeout = web_protocol.ceil_timeout

    def shortened_timeout(timeout):
        """Exercise the default deadline without waiting a minute. Preserve disabled timeouts."""
        return original_timeout(None if timeout is None else 0.01)

    monkeypatch.setattr(web_protocol, "ceil_timeout", shortened_timeout)

    async def scenario():
        """Hold an accepted request until admission has closed, then prove cleanup precedes copying."""
        started, release = asyncio.Event(), asyncio.Event()
        events = []

        def factory(*args, **kwargs):
            """Reuse the actual application and add one held request and a cleanup witness."""
            application = create_app(*args, **kwargs)

            async def held_write(request):
                """Represent an already accepted mutation that must finish before the copy."""
                started.set()
                await release.wait()
                events.append("write committed")
                return web.json_response({"saved": True})

            async def witness(_app):
                """Show when worker shutdown has drained the existing application."""
                yield
                events.append("workers stopped")

            application.router.add_post("/held-write", held_write)
            application.cleanup_ctx.append(witness)
            return application

        def copy(*args, **kwargs):
            """Reject the disposable copy after independently proving the full ordering."""
            assert events == ["write committed", "workers stopped"]
            events.append("copy started")
            raise OSError("simulated destination failure")

        app = create_managed_app(tmp_path, TOKEN, factory=factory, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            monkeypatch.setattr(maintenance, "migrate_archive", copy)
            pending = asyncio.create_task(client.post("/held-write", headers=HEADERS, json={}))
            await asyncio.wait_for(started.wait(), 5)
            host.preflight = ("isolated-copy-proof", None, None)
            response = await client.post(
                "/v1/storage/migration/start",
                headers=HEADERS,
                json={"check_id": "isolated-copy-proof", "overwrite": True},
            )
            assert response.status == 202
            assert not host.accepting
            assert events == []
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 503
            await asyncio.sleep(0.05)
            assert not host.task.done() and events == []
            release.set()
            assert (await pending).status == 200
            await asyncio.wait_for(host.task, 5)
            assert events == ["write committed", "workers stopped", "copy started"]
            assert host.status()["can_restore"] and not host.status()["released"]

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["intent", "release", "selection", "startup-completion"])
def test_post_replace_failure_preserves_release_boundary_and_closes_admission(
    tmp_path, monkeypatch, boundary
):
    """A visible replacement followed by a durability error must not restore stale release state or serve writes."""
    from open_energy_planner.runtime import maintenance
    from open_energy_planner.storage.adapters.lifecycle import maintenance as lifecycle
    from open_energy_planner.storage.adapters.lifecycle.files import BootstrapDurabilityError

    async def scenario():
        """Fail after the real file replacement at each boundary, then inspect recovery and restart state."""
        if boundary != "intent":
            journal(tmp_path, target_url=None, released=boundary == "startup-completion")
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            original_write = lifecycle.atomic_json

            def fail_after_replace(target, document, **options):
                """Retain the new visible document while reporting a failed directory durability flush."""
                original_write(target, document, **options)
                if (
                    boundary == "intent"
                    and document.get("phase") == "preparing"
                    or boundary == "release"
                    and document.get("released")
                    or boundary == "selection"
                    and target.name == "database-selection.json"
                    or boundary == "startup-completion"
                    and document.get("phase") == "completed"
                ):
                    raise BootstrapDurabilityError("fixture directory flush failed")

            monkeypatch.setattr(lifecycle, "atomic_json", fail_after_replace)
            if boundary == "intent":
                host.preflight = ("durability-proof", None, SECRET_URL)
                response = await client.post(
                    "/v1/storage/migration/start",
                    headers=HEADERS,
                    json={"check_id": "durability-proof", "overwrite": True},
                )
                assert response.status == 503
                assert "restart OEP" in await response.text()
            elif boundary == "startup-completion":
                await host.restart()
            else:
                monkeypatch.setattr(maintenance, "migrate_archive", lambda *args, **kwargs: {})
                await host.transfer()
            saved = read_private(tmp_path, JOURNAL_FILE)
            assert saved["released"] is (boundary != "intent")
            assert host.journal["released"] == saved["released"]
            assert not host.accepting and host.runner is None and host.engine is None
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 503
            if boundary != "selection":
                assert host.bootstrap_error and not host.status()["can_restore"]
                assert host.journal == saved
            else:
                assert host.status()["can_retry"] and not host.status()["can_restore"]
            reopened = maintenance.MaintenanceHost(
                tmp_path, TOKEN, create_app, {"source": "home_assistant"}
            )
            assert reopened.journal["released"] == saved["released"]

    asyncio.run(scenario())


def test_directory_flush_error_reports_visible_bootstrap_replacement(tmp_path, monkeypatch):
    """The persistence helper distinguishes a post-replacement directory error from an unchanged file."""
    import os
    from types import SimpleNamespace

    from open_energy_planner.storage.adapters.lifecycle import files

    def fail_directory_flush(descriptor):
        """Flush the real file normally and fail only the simulated parent directory descriptor."""
        if descriptor == -1:
            raise OSError("fixture directory failure")
        os.fsync(descriptor)

    monkeypatch.setattr(
        files,
        "os",
        SimpleNamespace(
            name="posix",
            chmod=os.chmod,
            replace=os.replace,
            fsync=fail_directory_flush,
            open=lambda *args: -1,
            close=lambda descriptor: None,
            O_RDONLY=os.O_RDONLY,
            O_DIRECTORY=0,
        ),
    )
    target = tmp_path / "bootstrap.json"
    target.write_text('{"released": false}', encoding="utf-8")
    with pytest.raises(files.BootstrapDurabilityError):
        files.atomic_json(target, {"released": True})
    assert read_private(tmp_path, "bootstrap.json") == {"released": True}


@pytest.mark.parametrize("boundary", ["interrupted-boot", "runtime-boot", "transfer", "restart"])
def test_unwritable_failure_journal_keeps_maintenance_reachable(tmp_path, monkeypatch, boundary):
    """A full volume during failure reporting cannot stop the listener or hide source recovery."""
    from open_energy_planner.runtime import maintenance

    journal(
        tmp_path, target_url=None, phase="restored" if boundary == "runtime-boot" else "copying"
    )

    def fail_runtime(*args, **kwargs):
        """Represent an unavailable runtime while retaining the independent maintenance listener."""
        raise OSError("fixture database unavailable")

    async def scenario():
        """Fail journal writes before startup or during an owned task and read real HTTP recovery status."""
        if boundary in {"interrupted-boot", "runtime-boot"}:
            monkeypatch.setattr(maintenance, "write_private", fail_runtime)
        factory = fail_runtime if boundary == "runtime-boot" else create_app
        app = create_managed_app(tmp_path, TOKEN, factory=factory, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            if boundary in {"transfer", "restart"}:
                monkeypatch.setattr(maintenance, "write_private", fail_runtime)
                if boundary == "transfer":
                    monkeypatch.setattr(maintenance, "migrate_archive", fail_runtime)
                    await host.transfer()
                else:
                    monkeypatch.setattr(host, "factory", fail_runtime)
                    await host.restart()
            assert (await client.get("/data-storage")).status == 200
            status = await (await client.get("/v1/storage/migration", headers=HEADERS)).json()
            assert status["phase"] == "failed" and not status["running"]
            assert status["can_retry"] and status["can_restore"]
            assert "free space and write permissions" in status["error"]
            assert read_private(tmp_path, JOURNAL_FILE)["released"] is False
            assert host.runner is None and host.engine is None

    asyncio.run(scenario())


@pytest.mark.parametrize("released", [False, True])
def test_supervisor_rescue_is_idempotent_and_never_reverts_a_released_target(tmp_path, released):
    """A retained rescue option may restore an interrupted source once but cannot discard post-release data."""
    from open_energy_planner.storage.adapters.lifecycle.maintenance import requested_recovery

    journal(tmp_path, released=released)
    assert requested_recovery(tmp_path) is not released
    assert requested_recovery(tmp_path) is False
    status = read_private(tmp_path, JOURNAL_FILE)
    assert status["phase"] == ("copying" if released else "restored")


def test_gateway_preserves_compressed_json_request_behavior(tmp_path):
    """A body decoded by the public HTTP server must not be forwarded with its old gzip headers or length."""
    import gzip

    from aiohttp import web

    def factory(*args, **kwargs):
        """Add an authenticated JSON echo to observe the managed app's actual decoded input."""
        application = create_app(*args, **kwargs)

        async def echo(request):
            """Return parsed JSON after both HTTP layers without another manual decompressor."""
            return web.json_response(await request.json())

        application.router.add_post("/echo", echo)
        return application

    async def scenario():
        """Send a real compressed HTTP body through the public maintenance gateway."""
        app = create_managed_app(tmp_path, TOKEN, factory=factory, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/echo",
                data=gzip.compress(b'{"value": 17}'),
                headers={**HEADERS, "Content-Type": "application/json", "Content-Encoding": "gzip"},
            )
            assert response.status == 200
            assert await response.json() == {"value": 17}

    asyncio.run(scenario())


def test_gateway_leaves_large_request_limits_with_the_inner_route(tmp_path):
    """History-sized requests retain a larger route limit while ordinary API requests remain bounded."""
    from aiohttp import web

    def factory(*args, **kwargs):
        """Add ordinary and extended JSON routes using the same clone mechanism as production history APIs."""
        application = create_app(*args, **kwargs)

        async def echo(request):
            """Report the actual received payload size after route-owned admission."""
            reader = (
                request.clone(client_max_size=1048576)
                if request.path == "/extended-echo"
                else request
            )
            body = await reader.json()
            return web.json_response({"length": len(body["payload"])})

        application.router.add_post("/ordinary-echo", echo)
        application.router.add_post("/extended-echo", echo)
        return application

    async def scenario():
        """Compare the public gateway outcomes against independently specified route limits."""
        app = create_managed_app(tmp_path, TOKEN, factory=factory, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/extended-echo", headers=HEADERS, json={"payload": "x" * 300000}
            )
            assert response.status == 200
            assert await response.json() == {"length": 300000}
            assert (
                await client.post("/ordinary-echo", headers=HEADERS, json={"payload": "x" * 300000})
            ).status == 413
            assert (
                await client.post(
                    "/extended-echo", headers=HEADERS, json={"payload": "x" * 1100000}
                )
            ).status == 413

    asyncio.run(scenario())


def test_failed_intent_write_keeps_original_runtime_and_status_unchanged(tmp_path, monkeypatch):
    """A full or unwritable bootstrap volume must not enter maintenance or report a migration that never started."""
    from open_energy_planner.runtime import maintenance

    async def scenario():
        """Fail only private intent persistence after preflight and observe the original app still serving."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            host = app[MAINTENANCE]
            host.preflight = ("intent-write-proof", None, SECRET_URL)

            def fail_write(*args):
                """Represent bootstrap disk exhaustion before source shutdown or target rebuilding."""
                raise OSError("fixture disk full")

            monkeypatch.setattr(maintenance, "write_private", fail_write)
            response = await client.post(
                "/v1/storage/migration/start",
                headers=HEADERS,
                json={"check_id": "intent-write-proof", "overwrite": True},
            )
            assert response.status == 503
            assert "free space" in await response.text()
            status = await (await client.get("/v1/storage/migration", headers=HEADERS)).json()
            assert status["phase"] == "idle" and status["running"]
            assert host.task is None and not (tmp_path / "database-migration.json").exists()
            assert (await client.get("/v1/snapshot", headers=HEADERS)).status == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["task", "cancelled", "cleanup"])
def test_shutdown_always_drains_runtime_and_closes_session(tmp_path, monkeypatch, failure):
    """Task errors, task cancellation and runtime cleanup errors must never skip client-session cleanup."""
    from types import SimpleNamespace

    from open_energy_planner.runtime.maintenance import MaintenanceHost

    async def scenario():
        """Observe all shutdown owners and preserve the actual failure after cleanup attempts."""
        host = MaintenanceHost(tmp_path, TOKEN, create_app, {"source": "home_assistant"})
        events = []
        host.task = asyncio.get_running_loop().create_future()
        if failure == "cancelled":
            host.task.cancel()
        else:
            host.task.set_exception(OSError("fixture journal write failed"))

        async def stop():
            """Record the required runtime drain even when its own cleanup fails."""
            events.append("runtime stopped")
            if failure == "cleanup":
                raise RuntimeError("fixture runtime cleanup failed")

        async def close_session():
            """Record the final client owner without needing an external network connection."""
            events.append("session closed")

        monkeypatch.setattr(host, "stop_runtime", stop)
        host.session = SimpleNamespace(close=close_session)
        expected = (
            asyncio.CancelledError
            if failure == "cancelled"
            else RuntimeError
            if failure == "cleanup"
            else OSError
        )
        with pytest.raises(expected) as raised:
            await host.close()
        assert events == ["runtime stopped", "session closed"]
        if failure == "cleanup":
            assert isinstance(raised.value.__context__, OSError)

    asyncio.run(scenario())


def test_overlapping_preflight_is_rejected_without_replacing_first_check(tmp_path, monkeypatch):
    """An overlapping editor receives conflict while the original check retains authority to start."""
    from threading import Event

    from open_energy_planner.runtime import maintenance

    async def scenario():
        """Hold the real off-loop check, reject its competitor, then use the first returned check identifier."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        started, release = Event(), Event()

        def check(*args):
            """Hold a disposable preflight so the competing request cannot merely run afterward."""
            started.set()
            assert release.wait(5)
            return {"backend": "SQLite", "contains_oep_data": True}

        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(maintenance, "check_destination", check)
            first = asyncio.create_task(
                client.post("/v1/storage/migration/check", headers=HEADERS, json={"url": None})
            )
            assert await asyncio.to_thread(started.wait, 3)
            try:
                second = await asyncio.wait_for(
                    client.post("/v1/storage/migration/check", headers=HEADERS, json={"url": None}),
                    2,
                )
                assert second.status == 409
            finally:
                release.set()
            evidence = await (await first).json()
            assert app[MAINTENANCE].preflight[0] == evidence["check_id"]

            async def transfer():
                """Avoid an irrelevant self-copy while proving the original confirmation remains accepted."""

            monkeypatch.setattr(app[MAINTENANCE], "transfer", transfer)
            response = await client.post(
                "/v1/storage/migration/start",
                headers=HEADERS,
                json={"check_id": evidence["check_id"], "overwrite": True},
            )
            assert response.status == 202

    asyncio.run(scenario())
