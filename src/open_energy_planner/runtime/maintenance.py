"""Keep the maintenance UI alive while draining and restarting the database-owning app runtime."""

import asyncio
import hmac
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from open_energy_planner.app import DEFAULT_REQUEST_LIMIT_KIB, MIN_API_TOKEN_LENGTH
from open_energy_planner.core.units import BYTES_PER_KIB
from open_energy_planner.frontend.assets import is_public
from open_energy_planner.frontend.assets import register as register_assets
from open_energy_planner.ingress import INGRESS_PEER, prefix
from open_energy_planner.runtime.diagnostics.logging import failure_trace
from open_energy_planner.storage.adapters.lifecycle.files import BootstrapDurabilityError
from open_energy_planner.storage.adapters.lifecycle.maintenance import (
    JOURNAL_FILE,
    DestinationCheckError,
    MigrationPhase,
    activate,
    check_candidate,
    check_destination,
    connection_description,
    describe,
    open_runtime_engine,
    read_private,
    recover_source,
    selected_url,
    write_private,
)
from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import archive_engine
from open_energy_planner.storage.writes import complete_write

MAINTENANCE = web.AppKey("storage-maintenance", object)
MAX_REQUEST_BYTES = DEFAULT_REQUEST_LIMIT_KIB * BYTES_PER_KIB
PROXY_TIMEOUT_SECONDS = 300
HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
ACTIVE_PHASES = frozenset(
    {
        MigrationPhase.PREPARING,
        MigrationPhase.COPYING,
        MigrationPhase.VERIFYING,
        MigrationPhase.CHECKING,
        MigrationPhase.RESTARTING,
    }
)


class MaintenanceHost:
    """Own one runtime, one serialized migration and a private journal. Never hot-swap its stores."""

    def __init__(self, root, token, factory, options):
        """Retain composition parameters, not database-backed service objects across restarts."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token, self.factory, self.options = token, factory, options
        self.gateway_token = uuid4().hex
        self.configured_url = os.environ.get("OEP_DATABASE_URL")
        self.bootstrap_error = False
        self.durability_error = False
        try:
            self.journal = read_private(root, JOURNAL_FILE)
            describe(selected_url(root, self.configured_url))
            if self.journal is not None:
                if type(self.journal["released"]) is not bool or self.journal[
                    "phase"
                ] not in ACTIVE_PHASES | {
                    MigrationPhase.COMPLETED,
                    MigrationPhase.RESTORED,
                    MigrationPhase.FAILED,
                    MigrationPhase.INTERRUPTED,
                }:
                    raise ValueError("Invalid maintenance journal")
                describe(self.journal["source_url"])
                describe(self.journal["target_url"])
        except (OSError, ValueError, TypeError, KeyError):
            self.journal = None
            self.bootstrap_error = True
        self.runner = self.session = self.task = self.engine = None
        self.upstream = None
        self.accepting = False
        self.lock = asyncio.Lock()
        self.preflight = None
        self.progress = None

    def record(self, **changes):
        """Replace private recovery evidence before crossing any externally meaningful boundary."""
        if self.bootstrap_error:
            raise OSError("Private recovery state needs repair before further writes")
        previous_phase = (self.journal or {}).get("phase")
        journal = {**(self.journal or {}), **changes}
        try:
            write_private(self.root, JOURNAL_FILE, journal)
        except BootstrapDurabilityError:
            # Replacement already happened. Never overwrite its release boundary from stale memory.
            self.journal = journal
            self.bootstrap_error = self.durability_error = True
            self.accepting = False
            logging.getLogger(__name__).error(
                "Database recovery metadata could not be confirmed durable. Runtime admission is closed. Check the application storage and restart before retrying recovery."
            )
            raise
        self.journal = journal
        if journal.get("phase") != previous_phase:
            phase = journal.get("phase")
            logging.getLogger(__name__).log(
                logging.WARNING
                if phase in {MigrationPhase.FAILED, MigrationPhase.INTERRUPTED}
                else logging.INFO,
                "Database maintenance phase: %s. %s",
                phase,
                "Review database recovery options before retrying. Normal operation remains paused."
                if phase in {MigrationPhase.FAILED, MigrationPhase.INTERRUPTED}
                else "The selected database is verified and the runtime has started."
                if phase in {MigrationPhase.COMPLETED, MigrationPhase.RESTORED}
                else "Normal operation resumes only after database verification and runtime startup.",
            )

    def record_failure(self, message):
        """Keep recovery reachable when recording a failure itself encounters a storage error."""
        if self.bootstrap_error:
            return
        try:
            self.record(phase=MigrationPhase.FAILED, error=message)
        except BootstrapDurabilityError:
            return
        except OSError:
            self.journal = {
                **self.journal,
                "phase": MigrationPhase.FAILED,
                "error": "Database recovery status could not be saved. Check free space and write permissions on the application data volume, then retry recovery.",
            }

    def status(self):
        """Project durable phases and truthful progress without credentials or raw exceptions."""
        journal = self.journal or {}
        progress = self.progress or {}
        return {
            "phase": MigrationPhase.FAILED
            if self.bootstrap_error
            else journal.get("phase", "idle"),
            "released": journal.get("released", False),
            "error": "Database recovery state could not be durably saved. Check the application data volume, repair its storage error and restart the app to read the retained recovery state."
            if self.durability_error
            else "Private database recovery settings are unreadable. Restore the bootstrap backup before starting the app."
            if self.bootstrap_error
            else journal.get("error"),
            "running": self.accepting,
            "backend": "Unknown"
            if self.bootstrap_error
            else describe(
                journal["target_url"]
                if journal.get("released")
                else selected_url(self.root, self.configured_url)
            ),
            "source": connection_description(journal.get("source_url")) if journal else None,
            "destination": connection_description(journal.get("target_url")) if journal else None,
            "connection": None
            if self.bootstrap_error
            else connection_description(
                journal["target_url"]
                if journal.get("released")
                else selected_url(self.root, self.configured_url)
            ),
            "can_restore": bool(
                not self.bootstrap_error
                and journal
                and not journal["released"]
                and journal["phase"] in {MigrationPhase.FAILED, MigrationPhase.INTERRUPTED}
            ),
            "can_retry": bool(
                not self.bootstrap_error
                and journal
                and journal["phase"] in {MigrationPhase.FAILED, MigrationPhase.INTERRUPTED}
            ),
            **progress,
        }

    async def start_runtime(self, url, application=None):
        """Open only a loopback listener after constructing the selected app. Public admission stays closed."""
        options = {**self.options, "ingress_peer": "127.0.0.1"}
        try:
            if application is None:
                self.engine = await asyncio.to_thread(
                    open_runtime_engine,
                    self.root,
                    url,
                    migrate_database=os.environ.get("OEP_MIGRATE_DATABASE") == "1",
                )
                application = self.factory(
                    self.root, self.token, database_engine=self.engine, **options
                )
        except BaseException:
            await self.stop_runtime()
            raise

        @web.middleware
        async def private_gateway(request, handler):
            """A local socket alone grants no ingress trust. Require the process-private proxy proof."""
            supplied = request.headers.get("X-OEP-Maintenance-Gateway", "")
            if not hmac.compare_digest(
                supplied.encode("utf-8", "replace"), self.gateway_token.encode()
            ):
                raise web.HTTPForbidden()
            return await handler(request)

        application.middlewares.insert(0, private_gateway)
        self.runner = web.AppRunner(application, shutdown_timeout=None)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.upstream = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        except BaseException:
            await self.stop_runtime()
            raise

    async def stop_runtime(self):
        """Close admission, drain handlers, planner and source workers, then dispose their engine."""
        self.accepting = False
        self.upstream = None
        try:
            if self.runner is not None:
                await self.runner.cleanup()
        finally:
            self.runner = None
            if self.engine is not None:
                await asyncio.to_thread(self.engine.dispose)
                self.engine = None

    async def boot(self):
        """Resume durable release intent. Leave interrupted, unreleased copies in maintenance."""
        self.session = ClientSession(
            timeout=ClientTimeout(total=PROXY_TIMEOUT_SECONDS), auto_decompress=False
        )
        if self.bootstrap_error:
            return
        if self.journal and self.journal["phase"] not in {
            MigrationPhase.COMPLETED,
            MigrationPhase.RESTORED,
        }:
            try:
                self.record(
                    phase=MigrationPhase.INTERRUPTED,
                    error="The app stopped before migration completed. Review recovery options.",
                )
            except BootstrapDurabilityError:
                return
            except OSError:
                self.record_failure(
                    "Interrupted migration status could not be saved. Check free space and write permissions on the application data volume, then retry recovery."
                )
            return
        try:
            if self.journal and self.journal["released"]:
                await asyncio.to_thread(activate, self.root, self.journal["target_url"])
            await self.start_runtime(selected_url(self.root, self.configured_url))
            self.accepting = True
        except Exception:
            if self.journal is None:
                raise
            self.record_failure(
                "The app could not start. Check database availability and retry startup."
            )

    async def transfer(self):
        """Drain fully, copy and verify off-loop, then persist release before starting any workers."""
        source = target = None
        application = None
        try:
            await self.stop_runtime()
            source = archive_engine(self.root, self.journal["source_url"])
            target = archive_engine(self.root, self.journal["target_url"])
            loop = asyncio.get_running_loop()

            def progress(phase, done, total):
                """Publish batch progress on the owning loop without writing the journal per row."""
                loop.call_soon_threadsafe(self.update_progress, phase, done, total)

            await asyncio.to_thread(
                migrate_archive, source, target, overwrite_target=True, progress=progress
            )
            self.record(phase=MigrationPhase.CHECKING)
            self.progress = None
            self.engine = await asyncio.to_thread(
                check_candidate, self.root, self.journal["target_url"]
            )
            application = self.factory(
                self.root,
                self.token,
                database_engine=self.engine,
                **{**self.options, "ingress_peer": "127.0.0.1"},
            )
            # Release intent precedes workers: a crash must never make old-source rollback appear safe.
            self.record(phase=MigrationPhase.RESTARTING, released=True)
            await asyncio.to_thread(activate, self.root, self.journal["target_url"])
            await self.start_runtime(self.journal["target_url"], application)
            self.record(phase=MigrationPhase.COMPLETED, error=None)
            self.accepting = True
        except Exception as error:
            failed_phase = self.journal["phase"]
            await self.stop_runtime()
            self.progress = None
            trace = failure_trace(error)
            logging.getLogger(__name__).exception(
                "Database maintenance failed (%s)\n%s",
                type(error).__name__,
                trace,
                exc_info=False,
            )
            self.record_failure(
                f"Migration stopped during {failed_phase}. "
                + (
                    "The new database is selected. Check its connection and retry app startup."
                    if self.journal["released"]
                    else "The destination was not released. Check database connections and permissions, then retry or restore the previous database."
                ),
            )
        finally:
            for engine in (source, target):
                if engine is not None:
                    await asyncio.to_thread(engine.dispose)

    def update_progress(self, phase, done, total):
        """Ignore queued callbacks after verification. Persist only phase changes."""
        if self.bootstrap_error or self.journal["phase"] not in {
            MigrationPhase.PREPARING,
            MigrationPhase.COPYING,
            MigrationPhase.VERIFYING,
        }:
            return
        if self.journal["phase"] != phase:
            self.record(phase=phase)
        self.progress = {
            "done": done,
            "total": total,
            "percent": round(done * 100 / total, 1) if total else None,
        }

    async def restart(self, restore=False):
        """Retry startup after release, or restore the source before release, without copying again."""
        try:
            await self.stop_runtime()
            if restore:
                await asyncio.to_thread(recover_source, self.root)
                self.journal = read_private(self.root, JOURNAL_FILE)
            elif self.journal["released"]:
                await asyncio.to_thread(activate, self.root, self.journal["target_url"])
            await self.start_runtime(selected_url(self.root, self.configured_url))
            self.record(
                phase=MigrationPhase.RESTORED if restore else MigrationPhase.COMPLETED, error=None
            )
            self.accepting = True
        except Exception as error:
            await self.stop_runtime()
            trace = failure_trace(error)
            logging.getLogger(__name__).exception(
                "Database runtime restart failed (%s)\n%s",
                type(error).__name__,
                trace,
                exc_info=False,
            )
            self.record_failure("The app could not start. Check the database connection and retry.")

    async def close(self):
        """Do not cancel a database thread mid-transfer during graceful process shutdown."""
        try:
            if self.task is not None:
                await self.task
        finally:
            try:
                await self.stop_runtime()
            finally:
                if self.session is not None:
                    await self.session.close()


def create_managed_app(directory, token, *, factory, **options):
    """Compose stable maintenance routing around the existing app without another service dependency."""
    if not isinstance(token, str) or len(token) < MIN_API_TOKEN_LENGTH:
        raise ValueError("A local API token of at least 24 characters is required")
    host = MaintenanceHost(directory, token, factory, options)

    @web.middleware
    async def gateway(request, handler):
        """Keep maintenance APIs local and sanitize trust headers before loopback forwarding."""
        maintenance_route = request.path.startswith("/v1/storage/migration")
        if maintenance_route:
            if not prefix(request) and not hmac.compare_digest(
                request.headers.get("Authorization", "").encode("utf-8", "replace"),
                f"Bearer {token}".encode(),
            ):
                raise web.HTTPUnauthorized()
            return await handler(request)
        if host.accepting:
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower()
                not in HOP_HEADERS
                | {"host", "x-ingress-path", "content-length", "content-encoding"}
            }
            headers["X-OEP-Maintenance-Gateway"] = host.gateway_token
            if trusted_prefix := prefix(request):
                headers["X-Ingress-Path"] = trusted_prefix
            try:
                async with host.session.request(
                    request.method,
                    host.upstream + str(request.rel_url),
                    headers=headers,
                    data=request.content.iter_any() if request.body_exists else None,
                    allow_redirects=False,
                ) as response:
                    outgoing = web.StreamResponse(
                        status=response.status,
                        headers={
                            name: value
                            for name, value in response.headers.items()
                            if name.lower() not in HOP_HEADERS
                        },
                    )
                    await outgoing.prepare(request)
                    async for chunk in response.content.iter_any():
                        await outgoing.write(chunk)
                    await outgoing.write_eof()
                    return outgoing
            except (ClientError, TimeoutError):
                raise web.HTTPServiceUnavailable(
                    text="The app is restarting. Open Data storage for migration status."
                ) from None
        if request.path == "/setup/session":
            return web.json_response(
                {"ingress": prefix(request) is not None, "build_info": {"status": "unavailable"}},
                headers={"Cache-Control": "no-store"},
            )
        if request.path in {"/", "/workspace"}:
            embedded = "?embedded=1" if request.query.get("embedded") == "1" else ""
            raise web.HTTPFound((prefix(request) or "") + "/data-storage" + embedded)
        if is_public(request):
            return await handler(request)
        raise web.HTTPServiceUnavailable(
            text="The app is in storage maintenance. Open Data storage."
        )

    app = web.Application(middlewares=[gateway], client_max_size=MAX_REQUEST_BYTES)
    app[MAINTENANCE] = host
    if options.get("ingress_peer"):
        app[INGRESS_PEER] = options["ingress_peer"]
    empty_configuration = SimpleNamespace(current=None, configuration=None)
    register_assets(app, empty_configuration, empty_configuration, empty_configuration)

    async def status(request):
        """Allow reconnecting browsers to read redacted durable status during runtime downtime."""
        return web.json_response(host.status(), headers={"Cache-Control": "no-store"})

    async def action(request):
        """Serialize explicit preflight, overwrite consent and recovery operations."""
        if host.lock.locked():
            raise web.HTTPConflict(text="A maintenance operation is already running")
        async with host.lock:
            if host.bootstrap_error:
                raise web.HTTPServiceUnavailable(text=host.status()["error"])
            if host.task is not None and not host.task.done():
                raise web.HTTPConflict(text="A maintenance operation is already running")
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise TypeError
                operation = request.match_info["action"]
                if operation == "check":
                    host.preflight = None
                    if not host.accepting:
                        raise ValueError
                    target_url = body["url"]
                    if target_url is not None and (
                        not isinstance(target_url, str) or not target_url.strip()
                    ):
                        raise ValueError
                    source_url = selected_url(host.root, host.configured_url)
                    evidence = {}

                    def probe():
                        """Finish alias-probe cleanup before cancellation can release the operation lock."""
                        evidence.update(check_destination(host.root, source_url, target_url))

                    cancellation = await complete_write(probe)
                    if cancellation is not None:
                        raise cancellation
                    check_id = uuid4().hex
                    host.preflight = (check_id, source_url, target_url)
                    return web.json_response(
                        {**evidence, "check_id": check_id}, headers={"Cache-Control": "no-store"}
                    )
                if operation == "start":
                    if (
                        not host.accepting
                        or body.get("overwrite") is not True
                        or host.preflight is None
                        or body.get("check_id") != host.preflight[0]
                    ):
                        raise ValueError
                    _, source_url, target_url = host.preflight
                    host.record(
                        source_url=source_url,
                        target_url=target_url,
                        released=False,
                        phase=MigrationPhase.PREPARING,
                        error=None,
                    )
                    host.accepting = False
                    host.preflight = None
                    host.task = asyncio.create_task(host.transfer())
                elif operation == "retry":
                    if (
                        host.journal is None
                        or host.accepting
                        or host.journal["phase"]
                        not in {MigrationPhase.FAILED, MigrationPhase.INTERRUPTED}
                    ):
                        raise ValueError
                    if host.journal["released"]:
                        host.record(phase=MigrationPhase.RESTARTING, error=None)
                        host.task = asyncio.create_task(host.restart())
                    else:
                        if body.get("overwrite") is not True:
                            raise ValueError
                        host.record(phase=MigrationPhase.PREPARING, error=None)
                        host.task = asyncio.create_task(host.transfer())
                elif operation == "restore":
                    if not host.status()["can_restore"]:
                        raise ValueError
                    host.record(phase=MigrationPhase.RESTARTING, error=None)
                    host.task = asyncio.create_task(host.restart(restore=True))
                else:
                    raise ValueError
            except DestinationCheckError as error:
                raise web.HTTPBadRequest(text=str(error)) from None
            except OSError:
                if host.bootstrap_error:
                    await host.stop_runtime()
                raise web.HTTPServiceUnavailable(
                    text=host.status()["error"]
                    if host.bootstrap_error
                    else "Cannot save database maintenance state. Check free space and write permissions on the application data volume."
                ) from None
            except (ValueError, TypeError, KeyError):
                raise web.HTTPBadRequest(
                    text="Check the database settings and confirmation. The requested operation is unavailable."
                ) from None
            return web.json_response(
                host.status(), status=202, headers={"Cache-Control": "no-store"}
            )

    app.router.add_get("/v1/storage/migration", status)
    app.router.add_post("/v1/storage/migration/{action}", action)

    async def lifecycle(_app):
        """Keep the gateway serving independently of managed runtime failures."""
        try:
            await host.boot()
            yield
        finally:
            await host.close()

    app.cleanup_ctx.append(lifecycle)
    return app
