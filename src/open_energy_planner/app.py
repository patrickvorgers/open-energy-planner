"""Local authenticated app with explicitly separated synthetic and HA input modes."""

import asyncio
import hmac
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from aiohttp import web

from open_energy_planner.contracts.api import ContractAPI
from open_energy_planner.contracts.bonus_history import BonusBudgetPreview, BonusHistory
from open_energy_planner.contracts.planning_basis import ContractBasisSource
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.core.units import BYTES_PER_KIB, SECONDS_PER_MINUTE
from open_energy_planner.home_assistant.observation_wire import (
    configuration_document,
    observation_status,
    parse_configuration,
    parse_report,
)
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, register_lifecycle
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    DEFAULT_HISTORY_LIMIT,
    SqlPlanRepository,
)
from open_energy_planner.storage.counters import CounterStore

from .build_info import RELEASE
from .demand.api import register as register_demand
from .demand.runtime import DemandRuntime
from .demand.storage import DemandStore
from .frontend.assets import is_public
from .frontend.assets import register as register_assets
from .frontend.session import register as register_session
from .home_assistant.catalogue import HACatalogue
from .home_assistant.market_prices import price_request
from .installation.api import catalogue as installation_catalogue
from .installation.api import register as register_installation
from .installation.api import warnings as installation_warnings
from .installation.api.setup import register as register_setup
from .installation.basis import InstallationBasisSource
from .installation.storage import (
    InstallationStore,
)
from .installation.storage import (
    document as installation_document,
)
from .installation.storage import (
    sources as installation_sources,
)
from .live_inputs import LiveInputs
from .notifications.api import register as register_notifications
from .notifications.collection import NotificationCenter
from .optimization.storage import StrategyStore
from .optimization.strategy_api import register as register_strategy
from .prices.api import register as register_prices
from .runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    explain_planning_problem,
    planning_notifications,
)
from .solar.api import register as register_solar
from .solar.runtime import SolarRuntime
from .solar.storage import SolarStore
from .storage.adapters.documents import SqlDocuments
from .storage.writes import complete_write

MIN_API_TOKEN_LENGTH = 24
DEFAULT_REQUEST_LIMIT_KIB = 256
MAX_REQUEST_ID_LENGTH = 128
FORECAST_WORKER_POLL_SECONDS = 5
WEATHER_RETRY_SECONDS = 1800
PRICE_SCHEDULE_POLL_SECONDS = 30
WEATHER_REFRESH_SECONDS = 3600
DATABASE_URL_UNSET = object()
_LOGGER = logging.getLogger(__name__)


def create_app(
    directory,
    token,
    *,
    solver=None,
    source="synthetic",
    basis_source=None,
    price_factory=None,
    weather_factory=None,
    solar_weather_factory=None,
    demand_scorer=None,
    discovery_options=None,
    ingress_peer=None,
    database_engine=None,
    database_url=DATABASE_URL_UNSET,
    migrate_database=False,
):
    """Create the authenticated local API for an explicit synthetic or HA input mode.

    The shared async lock serializes live refresh and repository mutation, including
    threaded publication saves. HA mode requires independent future-input providers.
    An omitted database URL uses the deployment environment. Explicit None selects
    local SQLite, including after a UI-managed return from an external backend.
    Physical control remains outside this application. A separate app-owned
    mutation lock orders price retention against financial publication without
    holding provider fetching or live control state.
    """
    if not token or len(token) < MIN_API_TOKEN_LENGTH:
        raise ValueError("A local API token of at least 24 characters is required")
    if source not in ("synthetic", "home_assistant"):
        raise ValueError("Select an explicit supported input mode")
    repository = SqlPlanRepository(
        directory,
        engine=database_engine,
        database_url=os.environ.get("OEP_DATABASE_URL")
        if database_url is DATABASE_URL_UNSET
        else database_url,
        migrate_database=migrate_database or os.environ.get("OEP_MIGRATE_DATABASE") == "1",
    )
    contracts = ContractStore(SqlDocuments(repository.engine, "contracts"))
    bonus_counters = CounterStore(SqlDocuments(repository.engine, "bonus-counters"))
    demand_store = DemandStore(SqlDocuments(repository.engine, "demand"))
    demand = DemandRuntime(demand_store, scorer=demand_scorer)
    from open_energy_planner.appliances.cycles.service import ApplianceService

    appliances = ApplianceService(SqlDocuments(repository.engine, "planning"), demand.consumers)
    from open_energy_planner.appliances.ev.service import EVService

    ev = EVService(SqlDocuments(repository.engine, "planning"), demand.consumers, appliances)
    from open_energy_planner.appliances.thermal.service import ThermalService

    thermal = ThermalService(
        SqlDocuments(repository.engine, "planning"), demand.consumers, appliances, ev
    )
    solar = SolarRuntime(SolarStore(SqlDocuments(repository.engine, "solar")))
    installation = InstallationStore(SqlDocuments(repository.engine, "installation"))
    strategy = StrategyStore(SqlDocuments(repository.engine, "planning"))
    installation_basis = basis_source is None
    if installation_basis:
        basis_source = InstallationBasisSource(installation, demand, solar)
    pending_contract_id = uuid4().hex
    bonus_history = BonusHistory()
    bonus_preview = BonusBudgetPreview()
    lock = asyncio.Lock()
    setup_catalogue = HACatalogue()
    savings_mutation_lock = asyncio.Lock()
    planner = None
    live = None
    from open_energy_planner.optimization.adapters.process.worker import ProcessSolver

    from .planning import PlanningCoordinator, PlanningService, PublicationSuperseded
    from .synthetic import capture

    # Declared fixture devices remain available when planning inputs are invalidated.
    synthetic_batteries = capture().problem.batteries if source == "synthetic" else ()

    # Retained incompatible evidence remains archived and cannot become an active command.
    if repository.state["plan"] and (
        repository.state["plan"].get("release") != RELEASE
        or repository.state["plan"].get("schema") != 2
        or "input_sha256" not in repository.state["plan"]
    ):
        repository.state["plan"] = None
        repository.save()

    async def publish(document):
        """Recheck current live inputs under the state lock before saving a complete candidate.

        Restore the previous in-memory pointer if persistence fails. No other state
        mutation may run while the threaded save owns this lock.
        """
        async with lock:
            if live:
                live.refresh()
                reason = planner.publication_rejection(
                    document, datetime.now(UTC), live.current_snapshot
                )
                if reason:
                    if (
                        live.current_snapshot is not None
                        and planner.latest is not live.current_snapshot
                    ):
                        planner.submit(live.current_snapshot)
                    raise PublicationSuperseded(reason)
            previous = repository.state["plan"]
            if installation.current and solar.configuration and demand.configuration:
                from dataclasses import asdict

                from .savings.forecast import comparison
                from .savings.forecast_state import initial_state

                document["comparison_bindings"] = {
                    "terms": [asdict(energy_term) for energy_term in demand.configuration.terms],
                    "solar": [asdict(source_item) for source_item in solar.configuration.sources],
                    "batteries": [asdict(battery) for battery in installation.current.batteries],
                    **((setup_catalogue.value or {}).get("suggestions", {}).get("settlement", {})),
                }
                try:
                    reference_start = await asyncio.to_thread(
                        initial_state,
                        savings_runtime.store,
                        savings_runtime.documents,
                        repository,
                        document,
                        contracts.current,
                    )
                    document["reference_forecast"] = await asyncio.to_thread(
                        comparison, document, reference_start
                    )
                except (ValueError, TypeError, KeyError) as error:
                    document["reference_forecast_issue"] = str(error)
                except Exception as error:
                    logging.getLogger(__name__).exception(
                        "Reference forecast failed. Plan publication continues"
                    )
                    document["reference_forecast_issue"] = str(error)
                from .home_assistant.names import comparison_names

                document["comparison_names"] = comparison_names(
                    setup_catalogue.value,
                    thermal.state,
                    ev.state,
                    installation.current,
                    solar.configuration,
                )
                document["currency"] = contracts.current.currency if contracts.current else None
                document["market_area"] = (
                    contracts.current.market_area if contracts.current else None
                )
            from .forecast_quality.api import capture_sources

            capture_sources(document, demand, solar, setup_catalogue.value, ev, thermal)
            repository.state["plan"] = document
            try:
                await asyncio.to_thread(appliances.accept_plan, document)
                await save_state()
            except Exception:
                repository.state["plan"] = previous
                raise

    notification_center = NotificationCenter()

    def planning_status():
        """Share immutable coordinator evidence with notification providers, never its mutation methods."""
        return PlanningStatus(
            planner.status,
            planner.reason,
            planner.blocked_since,
            planner.block_reason,
            has_published=planner.published is not None,
        )

    def explain_problem(reason):
        """Let the responsible module explain a propagated cause for deduplicated planner logging."""
        return notification_center.explain(reason) or explain_planning_problem(reason)

    def explain_live_problem(reason, _detail=None):
        """Keep source-specific actionable context in both planning notices and transition logs."""
        from .home_assistant.notifications import explain_live_problem as explain_measurements

        return explain_measurements(
            live, reason, setup_catalogue.value
        ) or explain_planning_problem(reason)

    planner = PlanningCoordinator(
        PlanningService(solver or ProcessSolver()), publish, explain=explain_problem
    )
    notification_center.register(
        "planning",
        lambda: planning_notifications(planning_status(), notification_center.explain),
        explain=explain_live_problem,
        fallback=True,
    )
    if source == "home_assistant":
        # Recovery always needs fresh reports. Retained plans are history, not commands.
        repository.state["plan"] = None
        repository.save()
        live = LiveInputs(planner, repository, basis_source)
        live.prepare_snapshot = lambda snapshot, now: thermal.prepare(
            snapshot,
            demand,
            now,
            (setup_catalogue.value or {}).get("timezone", "UTC"),
        )
        saved = repository.state.get("measurement_config")
        if installation.current:
            live.install_configuration(installation_sources(installation.current))
        elif saved:
            live.configure(parse_configuration(saved, revision=saved["revision"]))
        else:
            live.refresh()

    @web.middleware
    async def authentication(request, handler):
        """Require the installation Bearer token before executing any API handler."""
        from .ingress import prefix

        if request.method in ("GET", "HEAD") and (
            is_public(request) or request.path == "/setup/session"
        ):
            return await handler(request)
        if not prefix(request) and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode("utf-8", "replace"),
            f"Bearer {token}".encode(),
        ):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def save_state():
        """Finish state persistence under the caller-held lock before propagating cancellation."""
        cancellation = await complete_write(repository.save)
        if cancellation is not None:
            raise cancellation

    @web.middleware
    async def persist_invalidations(request, handler):
        """Persist hard revocations off-loop before sending a response, even when a handler fails."""
        try:
            return await handler(request)
        finally:
            if repository.pending_invalidation:
                async with lock:
                    if repository.pending_invalidation:
                        try:
                            await save_state()
                        except OSError:
                            raise web.HTTPServiceUnavailable(
                                text="Plan archive unavailable. Commands revoked"
                            ) from None

    app = web.Application(
        middlewares=[authentication, persist_invalidations],
        client_max_size=DEFAULT_REQUEST_LIMIT_KIB * BYTES_PER_KIB,
    )
    from .build_info import register as register_build_info

    register_notifications(app, notification_center)
    register_build_info(app)
    if source == "home_assistant":
        from .build_info import BUILD_STATUS
        from .runtime.diagnostics.version_notifications import version_notifications

        def version_notices():
            """Borrow only the loaded-build owner's private current compatibility evidence."""
            status = app[BUILD_STATUS]()
            return version_notifications(
                status["blocking_mismatch"], available=status["status"] != "unavailable"
            )

        notification_center.register("versions", version_notices)
    from dataclasses import asdict

    from .storage.adapters import usage as storage_capacity

    async def data_storage(request):
        """Return authenticated capacity evidence without blocking planning or exposing paths."""
        usage = await asyncio.to_thread(storage_capacity.storage_usage, repository.engine)
        return web.json_response(
            {**asdict(usage), "status": usage.status}, headers={"Cache-Control": "no-store"}
        )

    app.router.add_get("/v1/storage", data_storage)
    from .ingress import INGRESS_PEER

    if ingress_peer:
        app[INGRESS_PEER] = ingress_peer
    from .pairing import Pairing, register_pairing

    async def archive_lifecycle(_app):
        """Flush background revocations and close last, after planning/source writers have drained."""

        async def flush_invalidations():
            """Bound idle work using the existing worker cadence. A failed flush never reinstates commands."""
            while True:
                async with lock:
                    if repository.pending_invalidation:
                        try:
                            await save_state()
                        except OSError:
                            logging.getLogger(__name__).error(
                                "Archive write failed. Commands remain revoked"
                            )
                await asyncio.sleep(FORECAST_WORKER_POLL_SECONDS)

        worker = asyncio.create_task(flush_invalidations())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            try:
                async with lock:
                    if repository.pending_invalidation:
                        await save_state()
            finally:
                await asyncio.to_thread(repository.close)

    app.cleanup_ctx.append(archive_lifecycle)

    pairing = Pairing(directory, repository.state["instance_id"])
    register_pairing(app, pairing)
    if discovery_options is not None:
        from .discovery import advertise

        advertise(app, pairing, directory, token, **discovery_options)
    prices = None
    contract_basis = ContractBasisSource(
        basis_source, lambda: prices, lambda: contracts.current, lambda: bonus_counters.current
    )
    if live and (
        installation.current is not None
        or price_factory is not None
        or contracts.current is not None
        or contracts.issue
    ):
        live.basis_source = contract_basis

    async def persist_then_install(save, candidate, install=None, *, contract_input=True):
        """Save under the caller-held app lock, then install before propagating cancellation.

        Drain repeated request cancellations while the storage thread owns its
        write. Writer failures propagate without installation or refresh. A
        successful save becomes authoritative before cancellation is re-raised.
        Contract-related writes activate contract inputs. Unrelated settings must
        pass contract_input=False to retain the existing future-input adapter.
        """
        cancellation = await complete_write(save, candidate)
        if install is not None:
            install(candidate)
        if live:
            if contract_input:
                live.basis_source = contract_basis
            live.refresh(force=True)
        _LOGGER.info(
            "Configuration saved (%s). Updated settings are now active%s.",
            type(candidate).__name__,
            ". Planning prerequisites have been rechecked" if live else "",
        )
        if cancellation is not None:
            raise cancellation

    contract_api = ContractAPI(
        app,
        contracts,
        bonus_counters,
        bonus_history,
        bonus_preview,
        live,
        setup_catalogue,
        lambda: prices,
        lock,
        persist_then_install,
        lambda request: snapshot(request),
        pending_contract_id,
        planning_status=planning_status,
    )

    build_demand = register_demand(
        app,
        demand,
        demand_store,
        live,
        setup_catalogue,
        lock,
        persist_then_install,
        lambda request: snapshot(request),
    )

    async def demand_lifecycle(_app):
        """Fit and archive in a background thread. Never block HTTP/HA on model work or provider fetching."""
        forecast_logger = BackgroundLog(_LOGGER, "Household demand forecast update")

        async def update():
            """Capture consistent input versions and install only still-current archived results."""
            while True:
                job = None
                try:
                    async with lock:
                        refresh_model_preferences()
                        job = demand.job(datetime.now(UTC))
                    if job is not None:
                        result = await build_demand(job)
                        async with lock:
                            if (
                                demand.install(result, datetime.now(UTC))
                                and demand.forecast is not None
                            ):
                                forecast_logger.success(
                                    f"A new consumption forecast is available. Model status: {demand.model_status.replace('_', ' ')}."
                                )
                except Exception:
                    async with lock:
                        if job is not None:
                            demand.failed(job[0], datetime.now(UTC))
                        retry_seconds = (
                            (demand.retry_after - datetime.now(UTC)).total_seconds()
                            if demand.retry_after and demand.retry_after > datetime.now(UTC)
                            else FORECAST_WORKER_POLL_SECONDS
                        )
                    forecast_logger.exception(
                        "The previous forecast keeps its original validity. Check storage and forecast status if this persists.",
                        retry_seconds=max(FORECAST_WORKER_POLL_SECONDS, round(retry_seconds)),
                    )
                await asyncio.sleep(FORECAST_WORKER_POLL_SECONDS)

        task = asyncio.create_task(update())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(demand_lifecycle)

    if weather_factory is not None and live is not None:

        async def weather_lifecycle(_app):
            """Fetch temperature independently from planning, with hourly success and 30-minute failure retries."""
            async with weather_factory() as weather_source:
                weather_logger = BackgroundLog(_LOGGER, "Temperature weather refresh")

                async def update_weather():
                    """Use HA's current location. Missing weather leaves calendar models available."""
                    last_location = None
                    while True:
                        delay = WEATHER_RETRY_SECONDS
                        async with lock:
                            location = live.report.location if live.report else None
                            configured = demand.configuration is not None
                            if location != last_location:
                                demand.weather(())
                                last_location = location
                        if configured and location is not None:
                            try:
                                temperatures = await weather_source.read(
                                    location, datetime.now(UTC)
                                )
                                async with lock:
                                    if live.report and live.report.location == location:
                                        demand.weather(temperatures)
                                        weather_logger.success(
                                            f"Received {len(temperatures)} temperature hours. Next refresh in {WEATHER_REFRESH_SECONDS / SECONDS_PER_MINUTE:g} minutes."
                                        )
                                delay = WEATHER_REFRESH_SECONDS
                            except Exception as error:
                                weather_logger.exception(
                                    "Existing valid forecasts retain their expiry. Calendar consumption forecasting does not require fresh temperature data.",
                                    retry_seconds=WEATHER_RETRY_SECONDS,
                                    expected=isinstance(error, ValueError),
                                    reason="invalid weather data"
                                    if isinstance(error, ValueError)
                                    else None,
                                )
                        else:
                            delay = PRICE_SCHEDULE_POLL_SECONDS
                        await asyncio.sleep(delay)

                task = asyncio.create_task(update_weather())
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        app.cleanup_ctx.append(weather_lifecycle)

    if price_factory is not None:

        async def price_lifecycle(_app):
            """Start cached-price refresh independently of request latency and solver timing."""
            nonlocal prices
            async with price_factory(repository.engine) as prices:
                prices.persistence_lock = savings_mutation_lock
                price_logger = BackgroundLog(_LOGGER, "Market price refresh")

                async def update_prices():
                    """Check due source requests without blocking HTTP. Diagnose unexpected failures."""
                    while True:
                        try:
                            await prices.refresh(datetime.now(UTC))
                            if price_logger.failure_key is not None:
                                price_logger.success("Price refresh processing is running again.")
                        except Exception:
                            price_logger.exception(
                                "Previously accepted prices keep their original expiry. Check source and storage status.",
                                retry_seconds=PRICE_SCHEDULE_POLL_SECONDS,
                            )
                        await asyncio.sleep(PRICE_SCHEDULE_POLL_SECONDS)

                task = asyncio.create_task(update_prices())
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        app.cleanup_ctx.append(price_lifecycle)

    register_prices(app, lambda: prices, contracts, live, lock)

    async def snapshot(request):
        """Refresh live validity under the state lock and return the current installation snapshot."""
        async with lock:
            await asyncio.to_thread(
                appliances.tick,
                datetime.now(UTC),
                repository.state["enabled"],
                repository.state["plan"],
            )
            if live:
                live.refresh()
            pending_history = contract_api.history_request()
            devices = installation_catalogue(
                installation, solar, live.current_snapshot if live else None
            )
            if installation_basis or installation.current is not None:
                devices = setup_catalogue.devices(devices)
            if devices is None:
                devices = (
                    []
                    if live
                    else [
                        {
                            "id": unit.device_id,
                            "kind": "battery",
                            "name": "Battery",
                            "configured": True,
                            "can_follow_grid": unit.can_follow_grid,
                        }
                        for unit in synthetic_batteries
                    ]
                )
            from .installation.commands import execution_document

            execution_only = request.headers.get("X-OEP-View") == "commands"
            return web.json_response(
                {
                    "release": RELEASE,
                    "settlement_request": await settlement_request(),
                    "settlement_bonus_request": settlement_bonus_request(),
                    "savings_status": {
                        "issue": savings_runtime.issue,
                        "physical_issue": savings_runtime.physical_issue,
                        "action_required": savings_runtime.attention,
                    },
                    **repository.state,
                    "plan": execution_document(repository.state["plan"])
                    if execution_only
                    else {
                        key: value
                        for key, value in repository.state["plan"].items()
                        if key
                        not in {
                            "reference_forecast",
                            "reference_forecast_issue",
                            "comparison_bindings",
                            "comparison_names",
                        }
                    }
                    if repository.state["plan"]
                    else None,
                    "source": source,
                    "market_price_request": price_request(prices, datetime.now(UTC))
                    if source == "home_assistant"
                    else None,
                    "forecast_quality_request": forecast_history_request(datetime.now(UTC))
                    if source == "home_assistant"
                    else None,
                    "demand": {
                        **demand.document(datetime.now(UTC)),
                        "request": demand.request(datetime.now(UTC))
                        or demand.consumers.request(demand.configuration, datetime.now(UTC)),
                    },
                    "consumers": demand.consumers.document(),
                    "appliances": appliances.document(
                        datetime.now(UTC), repository.state["enabled"], repository.state["plan"]
                    ),
                    "ev": ev.document(
                        datetime.now(UTC),
                        (setup_catalogue.value or {}).get("timezone", "UTC"),
                        setup_catalogue.value,
                        repository.state["plan"],
                    ),
                    "solar": solar.document(datetime.now(UTC)),
                    "thermal": thermal.document(
                        datetime.now(UTC), setup_catalogue.value, repository.state["plan"]
                    ),
                    "installation": {
                        "configuration": installation_document(installation.current),
                        "issue": installation.issue,
                        "devices": devices,
                        "warnings": installation_warnings(installation, solar),
                        "setup_discovery_requested": setup_catalogue.requested,
                    },
                    "bonus_history": {
                        "request": {
                            **pending_history.document(),
                            "version": pending_history.version,
                        }
                        if pending_history
                        else None,
                        "issue": bonus_counters.issue
                        or (contract_api.history_issue if pending_history else None),
                    },
                    "config_revision": live.configuration.revision
                    if live and live.configuration
                    else "demo-1",
                    "measurement_config": configuration_document(live.configuration)
                    if live and live.configuration
                    else None,
                    "observations": observation_status(live.configuration, live.report)
                    if live
                    else None,
                    "plan_invalidated": bool(live and repository.state["plan"] is None),
                    "planning": {
                        "status": planner.status,
                        "reason": planner.reason,
                        "requested_snapshot_id": planner.latest.snapshot_id
                        if planner.latest
                        else None,
                    },
                }
            )

    async def enabled(request):
        """Persist explicit enable intent and request a fresh round for a new resume request."""
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise web.HTTPBadRequest(text="enabled must be boolean")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= MAX_REQUEST_ID_LENGTH:
            raise web.HTTPBadRequest(text="A control request identifier is required")
        async with lock:
            if body["enabled"] and request_id != repository.state["last_control_id"]:
                if live:
                    live.refresh(force=True)
                else:
                    planner.submit(capture())
            repository.state["enabled"] = body["enabled"]
            repository.state["last_control_id"] = request_id
            await save_state()
        return await snapshot(request)

    async def acknowledge(request):
        """Record acceptance only for the current plan ID. Reject stale acknowledgements."""
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Invalid acknowledgement")
        async with lock:
            current = repository.state["plan"]
            if current is None or body.get("plan_id") != current["plan_id"]:
                raise web.HTTPConflict(text="Acknowledgement refers to a different plan")
            repository.state["acknowledged_plan"] = body["plan_id"]
            await save_state()
        return web.json_response({"accepted": True})

    async def startup(app):
        """Start synthetic planning only. Live mode waits for configured real observations."""
        if not live:
            planner.submit(capture())

    async def cleanup(app):
        """Drain planning before outer resources and lifecycle completion, including failed startup."""
        try:
            yield
        finally:
            await planner.close()

    async def replan(request):
        """Request a fresh live round or validate explicitly synthetic state overrides."""
        if live:
            async with lock:
                live.refresh(force=True)
            return await snapshot(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"initial_kwh", "first_load_kwh"}:
                raise ValueError("Only the synthetic initial state can be changed")
            initial = body.get("initial_kwh", 0)
            if type(initial) not in (int, float):
                raise ValueError("Invalid synthetic SoC")
            load = body.get("first_load_kwh", 0)
            if type(load) not in (int, float):
                raise ValueError("Invalid synthetic household input")
            snapshot_input = capture(initial_kwh=initial, first_load_kwh=load)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid synthetic input") from None
        async with lock:
            planner.submit(snapshot_input)
        return await snapshot(request)

    app.on_startup.append(startup)
    app.cleanup_ctx.append(cleanup)
    app.router.add_post("/v1/replan", replan)

    if live:

        async def configure_sources(request):
            """Validate and commit source replacement only if the editor's expected revision matches."""
            try:
                body = await request.json()
                candidate = parse_configuration(body)
            except (ValueError, TypeError, KeyError):
                raise web.HTTPBadRequest(text="Invalid source configuration") from None
            async with lock:
                if installation.current is not None:
                    raise web.HTTPConflict(
                        text="Edit measurement roles through installation settings"
                    )
                current_revision = live.configuration.revision if live.configuration else None
                if body.get("expected_revision") != current_revision:
                    raise web.HTTPConflict(
                        text="Source configuration changed. Reload before saving"
                    )
                await persist_then_install(
                    repository.store_sources,
                    candidate,
                    live.install_configuration,
                    contract_input=False,
                )
            return await snapshot(request)

        async def observations(request):
            """Validate a complete report against current source bindings before refreshing live planning."""
            async with lock:
                if live.configuration is None:
                    raise web.HTTPConflict(text="Configure measurement sources first")
                try:
                    from datetime import UTC, datetime

                    body = await request.json()
                    report = parse_report(body, live.configuration, datetime.now(UTC))
                    live.receive(report)
                except (ValueError, TypeError, KeyError):
                    raise web.HTTPBadRequest(text="Invalid observation report") from None
            return await snapshot(request)

        app.router.add_put("/v1/sources", configure_sources)
        app.router.add_post("/v1/observations", observations)

    async def plan_history(request):
        """List bounded immutable archive summaries without changing the active plan."""
        try:
            limit = int(request.query.get("limit", DEFAULT_HISTORY_LIMIT))
            items = await asyncio.to_thread(
                repository.list_plans, limit=limit, before=request.query.get("before")
            )
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid plan history query") from None
        except OSError:
            raise web.HTTPServiceUnavailable(text="Plan archive unavailable") from None
        return web.json_response(
            {"plans": items, "next_before": items[-1]["plan_id"] if len(items) == limit else None}
        )

    async def archived_plan(request):
        """Read original content for inspection only. Never reinstall archived commands."""
        try:
            plan = await asyncio.to_thread(repository.get_plan, request.match_info["plan_id"])
        except (OSError, ValueError):
            raise web.HTTPServiceUnavailable(text="Plan archive unavailable or invalid") from None
        if plan is None:
            raise web.HTTPNotFound(text="Archived plan not found")
        return web.json_response(plan)

    app.router.add_get("/v1/plans", plan_history)
    app.router.add_get("/v1/plans/{plan_id}", archived_plan)
    app.router.add_get("/v1/snapshot", snapshot)

    app.router.add_put("/v1/enabled", enabled)
    app.router.add_post("/v1/ack", acknowledge)
    setup_catalogue = register_setup(
        app,
        installation,
        demand,
        solar,
        lock,
        live,
        persist_then_install,
        required=installation_basis,
        catalogue=setup_catalogue,
        contracts=contracts,
        appliances=appliances,
    )
    register_session(app, setup_catalogue)
    register_assets(app, installation, demand, solar)
    from .demand.consumer_api import register as register_consumers

    register_consumers(app, demand, lock, ev, thermal)
    from open_energy_planner.appliances.cycles.api import register as register_appliances

    register_appliances(app, appliances, repository, live, lock)
    from open_energy_planner.appliances.ev.api import register as register_ev

    register_ev(app, ev, setup_catalogue, repository, live, lock)
    from open_energy_planner.appliances.thermal.api import register as register_thermal

    register_thermal(app, thermal, setup_catalogue, repository, live, lock)
    if live:
        live.requested_strategy = lambda: strategy.current
        live.strategy_issue = lambda: strategy.issue
    register_strategy(app, strategy, repository, live, lock, persist_then_install)
    register_solar(
        app,
        solar,
        live,
        lock,
        persist_then_install,
        snapshot,
        solar_weather_factory,
        setup_catalogue,
    )
    register_installation(
        app, installation, solar, live, lock, persist_then_install, snapshot, setup_catalogue
    )
    from .savings.api import register as register_savings

    settlement_request, savings_runtime, settlement_bonus_request = register_savings(
        app,
        repository,
        contracts,
        bonus_counters,
        installation,
        solar,
        demand,
        lock,
        setup_catalogue,
        lambda: live.report.location if live and live.report else None,
        mutation_lock=savings_mutation_lock,
    )
    from .forecast_selection.api import register as register_model_selection

    refresh_model_preferences = register_model_selection(
        app,
        repository,
        savings_runtime.store,
        contracts,
        bonus_counters,
        installation,
        demand,
        solar,
        strategy,
        lock,
        enabled=live is not None and installation_basis,
    )
    from .forecast_quality.api import register as register_forecast_quality

    forecast_history_request = register_forecast_quality(
        app, repository, demand, solar, setup_catalogue, lock
    )
    register_lifecycle(app, RELEASE, source)
    return app


def main():
    """Preserve the public module entrypoint while startup is owned by runtime."""
    from .runtime.entrypoint import main as start

    start()


if __name__ == "__main__":
    main()
