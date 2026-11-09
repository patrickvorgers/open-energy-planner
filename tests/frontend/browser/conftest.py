"""Own isolated app sites and responsive browser contexts, sharing only the Chromium process."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import pytest
from aiohttp import ClientSession, web
from playwright.sync_api import sync_playwright
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from open_energy_planner.app import create_app
from open_energy_planner.pairing import PAIRING_KEY
from open_energy_planner.prices.bootstrap import price_factory as build_price_sources
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.runtime import ConfiguredPrices
from tests.frontend.browser.helpers import (
    DEFAULT_BROWSER_TIME_ZONE,
    SAVINGS_BROWSER_TIME_ZONE,
    TOKEN,
)


@pytest.fixture(scope="session")
def documentation_url():
    """Serve the compiled wiki and provisioning assets together at the production project prefix."""
    artifact = Path("_site").resolve()
    assert (artifact / "index.html").is_file(), "Build docs/site/mkdocs.yml before browser tests"
    with TemporaryDirectory(dir="results/ci") as directory:
        shutil.copytree(artifact, Path(directory) / "open-energy-planner")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=directory)
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/open-energy-planner/"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.fixture
def app_site(request):
    """Run a fresh real HA-mode app on a background loop. Browser tests configure actual stores, not route mocks."""
    ready, shutdown = Event(), Event()
    result = {}
    with TemporaryDirectory() as directory:
        if getattr(request, "param", None) == "documentation":
            from tests.frontend.browser.documentation_fixture import seed

            seed(Path(directory))
        if getattr(request, "param", None) == "device_planning":
            from tests.frontend.browser.device_planning_fixture import seed as seed_devices

            seed_devices(Path(directory))

        async def serve():
            """Own the app lifecycle and close its forecast/solver tasks before deleting the temporary store."""
            ingress = getattr(request, "param", None) in ("ingress", "activity_ingress")
            if getattr(request, "param", None) in (
                "savings",
                "savings_corrected",
                "activity_plans",
                "activity_ingress",
            ):
                from tests.frontend.browser.savings_fixture import seed

                seed(Path(directory), corrected=request.param == "savings_corrected")

            @asynccontextmanager
            async def price_sources(database_engine):
                """Reuse production three-source selection and HTTP save ownership with offline normalized inputs."""
                corrupt = getattr(request, "param", None) == "prices_corrupt"

                def fail_selection(_connection, _cursor, statement, _parameters, _context, _many):
                    """Expose an unreadable selection table without altering the shared configuration archive."""
                    if "FROM oep_price_decisions" in statement:
                        raise OperationalError(
                            "fixture selection outage", None, Exception("fixture")
                        )

                async def read_area():
                    """Keep the fixture's market explicit without creating a supplier configuration."""
                    return "NL", None

                @asynccontextmanager
                async def opened(area):
                    """Replace network fetching only, preserving real composition, storage, selection and API writes."""
                    async with build_price_sources(area)(database_engine) as service:
                        service.published = service.forecast = service.alternative = None
                        service.extra_forecasts = ()
                        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
                        service.series = tuple(
                            PriceSeries(
                                source,
                                area,
                                "EUR",
                                now,
                                now,
                                now + timedelta(hours=6),
                                tuple(
                                    PricePoint(
                                        now + timedelta(minutes=index * resolution),
                                        now + timedelta(minutes=(index + 1) * resolution),
                                        value,
                                        kind,
                                        resolution,
                                    )
                                    for index in range(hours * 60 // resolution)
                                ),
                            )
                            for source, hours, kind, resolution, value in (
                                ("nord_pool", 30, PriceKind.PUBLISHED, 60, 0.1),
                                ("energy_price_forecast_eu", 48, PriceKind.FORECAST, 60, 0.1),
                                ("epex_predictor", 72, PriceKind.FORECAST, 60, 0.1),
                                ("jeroen_nl", 72, PriceKind.FORECAST, 15, 0.1),
                            )
                        )
                        await service.refresh(datetime.now(UTC))
                        yield service

                if corrupt:
                    event.listen(database_engine, "before_cursor_execute", fail_selection)
                runtime = ConfiguredPrices(read_area, opened)
                try:
                    await runtime.configure()
                    yield runtime
                finally:
                    await runtime.close()
                    if corrupt:
                        event.remove(database_engine, "before_cursor_execute", fail_selection)

            application_factory = create_app
            if getattr(request, "param", None) in {"maintenance", "maintenance_interrupted"}:
                from functools import partial

                from open_energy_planner.runtime.maintenance import create_managed_app

                application_factory = partial(create_managed_app, factory=create_app)
                if request.param == "maintenance_interrupted":
                    from open_energy_planner.storage.adapters.lifecycle.maintenance import (
                        write_private,
                    )
                    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
                        SqlPlanRepository,
                    )

                    SqlPlanRepository(directory).close()
                    write_private(
                        directory,
                        "database-migration.json",
                        {
                            "source_url": None,
                            "target_url": "postgresql+psycopg://fixture:fixture@unavailable.invalid/oep",
                            "released": False,
                            "phase": "copying",
                            "error": None,
                        },
                    )
            application = application_factory(
                Path(directory),
                TOKEN,
                source="home_assistant",
                ingress_peer="127.0.0.1" if ingress else None,
                price_factory=price_sources
                if str(getattr(request, "param", "")).startswith("prices")
                else None,
            )
            if getattr(request, "param", None) == "planning_performance":
                from tests.frontend.browser.performance_fixture import seed

                seed(Path(directory))
            if getattr(request, "param", None) in (
                "activity",
                "activity_plans",
                "activity_ingress",
            ):
                from tests.frontend.browser.activity_fixture import register as register_activity

                register_activity(application)
            if ingress:
                pairing = application[PAIRING_KEY]
                pairing.supervisor_secret = "browser-supervisor-private-proof"
                await pairing.supervisor_pair(
                    {
                        "peer_id": "browser-home-assistant",
                        "name": "Browser Home Assistant",
                        "secret": pairing.supervisor_secret,
                    }
                )
            if getattr(request, "param", None) == "pairing":
                pairing = application[PAIRING_KEY]
                pairing.fingerprint = "a" * 64
                pairing.request(
                    {
                        "peer_id": "browser-test-ha-0123456789",
                        "name": "Browser test Home Assistant",
                        "secret": "browser-claim-only-0123456789",
                    }
                )
            runner = web.AppRunner(application)
            await runner.setup()
            if getattr(request, "param", None) in (
                "activity",
                "activity_plans",
                "activity_ingress",
            ):
                from tests.frontend.browser.activity_fixture import seed as seed_activity

                await seed_activity(application)
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            result["url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            proxy_runner = None
            session = ClientSession()
            if ingress:
                upstream = result["url"]
                prefix = "/api/hassio_ingress/browser_test"

                async def forward(request):
                    """Use a real HTTP proxy to prove HTML, CSS, JS and API calls retain the ingress prefix."""
                    async with session.request(
                        request.method,
                        upstream + str(request.rel_url)[len(prefix) :],
                        data=await request.read(),
                        headers={
                            "X-Ingress-Path": prefix,
                            "Content-Type": request.headers.get("Content-Type", "application/json"),
                            **{
                                name: request.headers[name]
                                for name in ("X-OEP-Release", "X-OEP-Build", "X-OEP-View")
                                if name in request.headers
                            },
                        },
                    ) as response:
                        return web.Response(
                            body=await response.read(),
                            status=response.status,
                            headers={
                                "Content-Type": response.headers.get(
                                    "Content-Type", "application/json"
                                )
                            },
                        )

                proxy = web.Application()
                proxy.router.add_route("*", prefix + "/{path:.*}", forward)
                proxy_runner = web.AppRunner(proxy)
                await proxy_runner.setup()
                await web.TCPSite(proxy_runner, "127.0.0.1", 0).start()
                result["url"] = f"http://127.0.0.1:{proxy_runner.addresses[0][1]}{prefix}"
            ready.set()
            try:
                while not shutdown.is_set():
                    await asyncio.sleep(0.02)
            finally:
                await runner.cleanup()
                if proxy_runner:
                    await proxy_runner.cleanup()
                await session.close()

        thread = Thread(target=lambda: asyncio.run(serve()), daemon=True)
        thread.start()
        assert ready.wait(15), "App failed to start"
        try:
            yield result["url"]
        finally:
            shutdown.set()
            thread.join(timeout=15)
            assert not thread.is_alive()


@pytest.fixture(params=[{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def app_page(app_site, request, browser):
    """Open /setup in a fresh desktop or touch-sized mobile context of the shared browser.

    Each case owns its page and context. The app_site fixture owns a temporary
    store, removed after this context closes. The savings and savings_corrected
    cases use SAVINGS_BROWSER_TIME_ZONE. Others use DEFAULT_BROWSER_TIME_ZONE.
    """
    zone = (
        SAVINGS_BROWSER_TIME_ZONE
        if request.node.callspec.params.get("app_site") in ("savings", "savings_corrected")
        else DEFAULT_BROWSER_TIME_ZONE
    )
    with browser.new_context(
        viewport=request.param,
        has_touch=request.param["width"] < 500,
        timezone_id=zone,
        locale="en-US",
    ) as context:
        page = context.new_page()
        page.add_init_script("""(() => {
            const original = window.fetch;
            let pending = 0, waiters = [];
            window.__oepProductReadsIdle = () => new Promise(resolve => {
                if (!pending) resolve(); else waiters.push(resolve);
            });
            window.fetch = function(input, options) {
                const result = original.call(this, input, options);
                const url = new URL(input instanceof Request ? input.url : input, location.href);
                if (url.pathname.endsWith('/v1/events')) return result;
                pending++;
                result.then(response => response.clone().arrayBuffer()).catch(() => {}).finally(() => {
                    pending--;
                    if (!pending) {const ready = waiters; waiters = []; ready.forEach(resolve => resolve());}
                });
                return result;
            };
        })();""")
        page.goto(app_site + "/setup")
        yield page, app_site


@pytest.fixture(scope="session")
def browser():
    """Launch Chromium once per suite and close it even if a browser test fails."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()
