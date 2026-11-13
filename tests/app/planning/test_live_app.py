"""Authenticated real-HTTP observation ingress through the existing planner/solver."""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from open_energy_planner import synthetic
from open_energy_planner.app import create_app
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import SolveOptions, Strategy
from open_energy_planner.planning import CommandMapper
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.database.support.archive import list_plans, read_plan, read_state
from tests.ha_contract.test_observation_inputs import (
    basis,
    configuration,
    configuration_document,
    report_body,
)

TOKEN = "local-test-only-0123456789abcdef"


def test_ev_forecast_blocker_logs_no_source_identity_but_keeps_authenticated_context(
    tmp_path, monkeypatch, caplog
):
    """Keep meter IDs out of logs while the authenticated EV view retains source context."""
    from open_energy_planner.appliances.ev.service import EVService
    from open_energy_planner.planning import PlanningCoordinator
    from tests.app.ev.support.devices import NOW, settings

    captured = {}
    original_ev_init = EVService.__init__
    original_planner_init = PlanningCoordinator.__init__

    def capture_ev(service, *arguments, **keywords):
        """Retain the composed service without replacing any production behavior."""
        original_ev_init(service, *arguments, **keywords)
        captured["ev"] = service

    def capture_planner(planner, *arguments, **keywords):
        """Retain the real coordinator and its app-owned explanation callback."""
        original_planner_init(planner, *arguments, **keywords)
        captured["planner"] = planner

    monkeypatch.setattr(EVService, "__init__", capture_ev)
    monkeypatch.setattr(PlanningCoordinator, "__init__", capture_planner)

    async def scenario():
        """Use normal startup and cleanup while injecting only synthetic private-value sentinels."""
        async with running_app(tmp_path, FutureSource()):
            ev = captured["ev"]
            ev.state = {**ev.state, **settings(), "assignments": {}}
            ev.state["points"][0]["energy_entity"] = "sensor.private_ev_meter_sentinel"
            ev.issue = "Charger component forecast unavailable. EV history cannot be removed safely"
            with caplog.at_level(logging.INFO, logger="open_energy_planner.planning"):
                captured["planner"].invalidate("ev_forecast_boundary_unavailable")
            assert "Planning blocked: ev_forecast_boundary_unavailable" in caplog.text
            assert "private_ev_meter_sentinel" not in caplog.text
            assert "sensor.charger_power" not in caplog.text
            assert (
                "sensor.private_ev_meter_sentinel" in ev.document(NOW, "Europe/Amsterdam")["issue"]
            )

    asyncio.run(scenario())


class FutureSource:
    """Explicit test forecast/price provider, never a production fallback."""

    def __init__(self):
        """Initialize an explicitly test-only future-input cache and availability switch."""
        self.future = None
        self.available = True

    def current(self, config, now, *, report=None):
        """Return independently configured future inputs for the current revision or explicit unavailability."""
        if not self.available:
            return None
        if self.future is None:
            self.future = basis(config, now=now)
        current = synthetic.capture(now).problem
        soc = next(reading.value for reading in report.readings if reading.key == "battery.soc")
        unit = current.batteries[0]
        return replace(
            self.future,
            config_revision=config.revision,
            problem=replace(
                current,
                intervals=self.future.problem.intervals,
                batteries=(replace(unit, battery=replace(unit.battery, initial_kwh=soc / 100)),),
            ),
        )


class Solver:
    """Adapt real whole-watt HiGHS arithmetic to the asynchronous app-test port."""

    async def solve(self, problem, *, strategy=Strategy.COST):
        """Solve the supplied immutable problem with production whole-watt constraints."""
        return HighsSolver().solve(problem, SolveOptions(strategy=strategy))


@asynccontextmanager
async def running_app(path, future=None, solver=None):
    """Run an authenticated isolated live app over real local HTTP and always clean up its runner."""
    runner = web.AppRunner(
        create_app(
            path, TOKEN, source="home_assistant", basis_source=future, solver=solver or Solver()
        )
    )
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
            yield session, url
    finally:
        await runner.cleanup()


async def configure(session, url, revision=None):
    """Select explicit fixture sources against the expected revision and require successful app ownership."""
    async with session.put(
        url + "/v1/sources",
        json={
            "bindings": configuration_document(configuration())["bindings"],
            "expected_revision": revision,
        },
    ) as response:
        assert response.status == 200
        return await response.json()


async def send(session, url, config, *, soc=50, fault=False):
    # Wire configuration is mapped back to the immutable test contract for fixture creation.
    """Submit independently constructed normalized readings or a deliberate required-source fault."""
    selected = replace(configuration(), revision=config["revision"])
    body = report_body(selected, now=datetime.now(UTC), soc=soc)
    if fault:
        raw = body["readings"].pop()
        body["issues"].append(
            {"key": raw["key"], "entity_id": raw["entity_id"], "reason": "unavailable"}
        )
    async with session.post(url + "/v1/observations", json=body) as response:
        assert response.status == 200
        return await response.json()


async def wait_ready(session, url):
    """Poll boundedly for completed planning and require a ready validated candidate."""
    for _ in range(100):
        async with session.get(url + "/v1/snapshot") as response:
            state = await response.json()
        if state["planning"]["status"] in ("ready", "failed"):
            break
        await asyncio.sleep(0.01)
    assert state["planning"]["status"] == "ready", state["planning"]
    return state


def test_device_preparation_does_not_block_authenticated_plan_history(tmp_path, monkeypatch):
    """History stays readable over real HTTP while a snapshot owns slow appliance preparation."""
    from open_energy_planner.appliances.cycles.service import ApplianceService

    entered, release = threading.Event(), threading.Event()
    original_prepare = ApplianceService.prepare
    loop_thread = threading.get_ident()

    def slow_prepare(service, *args, **kwargs):
        """Hold the domain worker long enough to observe a concurrent history request."""
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return original_prepare(service, *args, **kwargs)

    async def scenario():
        """Read an authenticated cheap endpoint before permitting preparation to finish."""
        future = FutureSource()
        original_current = future.current

        def current_basis(*args, **kwargs):
            """Assert actual HTTP capture and publication revalidation use the source-owning loop."""
            assert threading.get_ident() == loop_thread
            return original_current(*args, **kwargs)

        monkeypatch.setattr(future, "current", current_basis)
        async with running_app(tmp_path, future) as (session, url):
            state = await configure(session, url)
            monkeypatch.setattr(ApplianceService, "prepare", slow_prepare)
            pending = asyncio.create_task(send(session, url, state["measurement_config"]))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                async with asyncio.timeout(1):
                    async with session.get(url + "/v1/plans?limit=1") as response:
                        assert response.status == 200
                        await response.json()
                assert not pending.done()
            finally:
                release.set()
            await pending
            ready = await wait_ready(session, url)
            archived = read_plan(tmp_path, ready["plan"]["plan_id"])
            assert archived["input_snapshot"]["price_version"] == future.future.price_version

    asyncio.run(scenario())


def test_manual_strategy_switch_retains_old_plan_until_valid_replacement(tmp_path):
    """A delayed consumption solve preserves cost commands/history. Only its complete accepted replacement changes strategy."""

    async def scenario():
        """Use real HTTP, active storage and a gated native solve to observe the pending transition."""
        gate, entered = asyncio.Event(), asyncio.Event()

        class DelayedSolver(Solver):
            """Pause only consumption rounds, leaving the baseline plan available."""

            async def solve(self, problem, *, strategy=Strategy.COST):
                """Wait for explicit release before solving the newly requested objective."""
                if strategy == Strategy.CONSUMPTION:
                    entered.set()
                    await asyncio.wait_for(gate.wait(), 5)
                return await super().solve(problem, strategy=strategy)

        async with running_app(tmp_path, FutureSource(), solver=DelayedSolver()) as (session, url):
            state = await configure(session, url)
            await send(session, url, state["measurement_config"])
            original = await wait_ready(session, url)
            plan = original["plan"]
            original_record = read_plan(tmp_path, plan["plan_id"])
            async with session.post(url + "/v1/ack", json={"plan_id": plan["plan_id"]}) as response:
                assert response.status == 200
            async with session.put(
                url + "/v1/strategy",
                json={"strategy": "minimize consumption", "expected_strategy": "minimize cost"},
            ) as response:
                assert response.status == 200
                status = await response.json()
            await asyncio.wait_for(entered.wait(), 5)
            assert status["requested"] == "minimize consumption"
            assert status == {"requested": "minimize consumption", "issue": None}
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["plan"] == plan
            async with session.put(
                url + "/v1/strategy",
                json={"strategy": "minimize cost", "expected_strategy": "minimize cost"},
            ) as response:
                assert response.status == 409
            gate.set()
            replacement = await wait_ready(session, url)
            assert replacement["plan"]["strategy"] == "minimize consumption"
            assert replacement["plan"]["config_revision"] == plan["config_revision"]
            assert replacement["plan"]["consumption_objective"]["proven_optimum_kwh"] >= 0
            assert read_plan(tmp_path, plan["plan_id"]) == original_record
            async with session.get(url + "/v1/strategy") as response:
                status = await response.json()
            assert status == {"requested": "minimize consumption", "issue": None}
            async with session.post(
                url + "/v1/ack", json={"plan_id": replacement["plan"]["plan_id"]}
            ) as response:
                assert response.status == 200
            async with session.get(url + "/v1/strategy") as response:
                assert await response.json() == {"requested": "minimize consumption", "issue": None}

    asyncio.run(scenario())


def test_publication_revalidates_inputs_without_logging_expected_invalidation_as_error(
    tmp_path, monkeypatch, caplog
):
    """Lose forecasts after candidate validation. Discard it, preserve history and recover without a false ERROR."""

    async def scenario():
        """Invalidate exactly between mapping a valid candidate and its locked publication."""
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            state = await configure(session, url)
            await send(session, url, state["measurement_config"])
            original = await wait_ready(session, url)
            archive = original["plan"]["plan_id"]
            archived = read_plan(tmp_path, archive)
            archives = list_plans(tmp_path)
            build = CommandMapper.build
            mapped = asyncio.Event()
            loop = asyncio.get_running_loop()

            def invalidate_after_mapping(mapper, *args):
                """Remove future inputs only once the candidate has passed independent checks."""
                document = build(mapper, *args)
                future.available = False
                loop.call_soon_threadsafe(mapped.set)
                return document

            monkeypatch.setattr(CommandMapper, "build", invalidate_after_mapping)
            caplog.clear()
            async with session.post(url + "/v1/replan") as response:
                assert response.status == 200
            await asyncio.wait_for(mapped.wait(), 5)
            assert list_plans(tmp_path) == archives
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"]["status"] == "not_ready"
            assert list_plans(tmp_path) == archives
            assert read_plan(tmp_path, archive) == archived
            assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
            monkeypatch.setattr(CommandMapper, "build", build)
            future.available = True
            async with session.post(url + "/v1/replan") as response:
                assert response.status == 200
            await wait_ready(session, url)

    asyncio.run(scenario())


def test_synthetic_submission_waits_for_inflight_publication(tmp_path, monkeypatch):
    """Block a synthetic publication save. A newer replan must wait before replacing the coordinator generation."""

    async def scenario():
        """Use real HTTP and thread events to exercise the publication/submission lock boundary."""
        from open_energy_planner.planning import PlanningCoordinator

        captured = asyncio.Event()
        original_capture = synthetic.capture

        def observe_capture(*args, **kwargs):
            """Signal arrival of the second HTTP request before it waits for publication ownership."""
            result = original_capture(*args, **kwargs)
            if kwargs.get("initial_kwh") == 0.5:
                captured.set()
            return result

        monkeypatch.setattr(synthetic, "capture", observe_capture)
        async with (
            TestServer(create_app(tmp_path, TOKEN, solver=Solver())) as server,
            ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        ):
            url = str(server.make_url("/")).rstrip("/")
            await wait_ready(session, url)
            started, release = threading.Event(), threading.Event()
            submissions = []
            save, submit = SqlPlanRepository.save, PlanningCoordinator.submit

            def slow_save(repository):
                """Keep publication inside its lock until the test releases disk persistence."""
                started.set()
                if not release.wait(5):
                    raise AssertionError("Publication save was not released")
                save(repository)

            def observe_submit(coordinator, snapshot):
                """Record whether the newer generation bypassed the outstanding publication."""
                submissions.append(release.is_set())
                submit(coordinator, snapshot)

            monkeypatch.setattr(SqlPlanRepository, "save", slow_save)
            async with session.post(url + "/v1/replan", json={"initial_kwh": 0.25}) as response:
                assert response.status == 200
            assert await asyncio.to_thread(started.wait, 5)
            monkeypatch.setattr(PlanningCoordinator, "submit", observe_submit)
            pending = asyncio.create_task(
                session.post(url + "/v1/replan", json={"initial_kwh": 0.5})
            )
            try:
                await asyncio.wait_for(captured.wait(), 5)
                assert not submissions
                assert not pending.done()
            finally:
                release.set()
                response = await pending
                await response.read()
                response.release()
            assert response.status == 200
            assert submissions == [True]
            await wait_ready(session, url)

    asyncio.run(scenario())


@pytest.mark.parametrize(("soc", "allowed"), [(49, True), (99, False)])
def test_publication_checks_new_soc_even_without_a_queued_round(
    tmp_path, monkeypatch, soc, allowed
):
    """During a cheap-charge solve, a 49% reading leaves room until the next quarter plus grace. 99% lacks space and must trigger a new round."""

    async def scenario():
        """Hold the solver while real HTTP delivers a newer measurement below the ordinary replan interval."""
        async with running_app(tmp_path, FutureSource()) as (session, url):
            state = await configure(session, url)
            config = state["measurement_config"]
            await send(session, url, config)
            await wait_ready(session, url)
            started, release = asyncio.Event(), asyncio.Event()
            solve, build = Solver.solve, CommandMapper.build
            mapped = []

            async def delayed_solve(solver, problem):
                """Keep input delivery possible during the active calculation."""
                started.set()
                await release.wait()
                return await solve(solver, problem)

            def observe_mapping(mapper, *args):
                """Retain candidate identities to distinguish rejected and persisted documents."""
                document = build(mapper, *args)
                mapped.append(document)
                return document

            monkeypatch.setattr(Solver, "solve", delayed_solve)
            monkeypatch.setattr(CommandMapper, "build", observe_mapping)
            async with session.post(url + "/v1/replan") as response:
                before = await response.json()
            await asyncio.wait_for(started.wait(), 5)
            try:
                after = await send(session, url, config, soc=soc)
                assert (
                    after["planning"]["requested_snapshot_id"]
                    == before["planning"]["requested_snapshot_id"]
                )
            finally:
                release.set()
            await wait_ready(session, url)
            assert mapped
            assert (read_plan(tmp_path, mapped[0]["plan_id"]) is not None) is allowed
            assert len(mapped) == (1 if allowed else 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("save_fails", [False, True])
def test_live_replan_waits_for_publication_save_before_invalidating(
    tmp_path, monkeypatch, save_fails
):
    """Block successful/failing publication saves. Require concurrent invalidation to wait and finally clear memory/disk pointers."""

    async def scenario():
        """Release the held publication independently of either HTTP response's scheduling."""
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            state = await configure(session, url)
            await send(session, url, state["measurement_config"])
            original = await wait_ready(session, url)
            archive = original["plan"]["plan_id"]
            archived = read_plan(tmp_path, archive)
            started, release = threading.Event(), threading.Event()
            save = SqlPlanRepository.save
            blocked = False

            def slow_save(repository):
                nonlocal blocked
                if not blocked:
                    blocked = True
                    started.set()
                    if not release.wait(5):
                        raise AssertionError("Publication save was not released")
                    if save_fails:
                        raise OSError("Injected publication failure")
                else:
                    assert release.is_set(), "Concurrent repository writes"
                save(repository)

            monkeypatch.setattr(SqlPlanRepository, "save", slow_save)
            # The initial snapshot may wait behind the publication writer too.
            publication = asyncio.create_task(session.post(url + "/v1/replan"))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                future.available = False
                pending = asyncio.create_task(session.post(url + "/v1/replan"))
                await asyncio.sleep(0.05)
                assert not pending.done(), "Invalidation bypassed the publication lock"
            finally:
                release.set()
            async with await publication as response:
                assert response.status == 200
            async with await pending as response:
                assert response.status == 200
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"] == {
                "status": "not_ready",
                "reason": "forecast_or_prices_unavailable",
                "requested_snapshot_id": None,
            }
            assert read_state(tmp_path)["plan"] is None
            assert read_plan(tmp_path, archive) == archived

    asyncio.run(scenario())


def test_real_measurements_reach_solver_preserved_plan_and_known_fault_revokes_it(tmp_path):
    """Forward measured SoC into real optimization. Retain evidence, then revoke commands on a known required-source fault."""

    async def scenario():
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            state = await configure(session, url)
            config = state["measurement_config"]
            assert state["plan"] is None
            await send(session, url, config, soc=75)
            state = await wait_ready(session, url)
            plan = state["plan"]
            assert plan["input_snapshot"]["source"] == "home_assistant"
            assert (
                plan["input_snapshot"]["problem"]["batteries"][0]["battery"]["initial_kwh"] == 0.75
            )
            assert plan["input_snapshot"]["forecast_version"] == "forecast-1"
            assert plan["input_snapshot"]["price_version"] == "prices-1"
            assert plan["input_snapshot"]["measurement_evidence"][1]["entity_id"] == "sensor.soc"
            archived = plan["plan_id"]
            original = read_plan(tmp_path, archived)
            state = await send(session, url, config, fault=True)
            assert state["plan"] is None
            assert state["plan_invalidated"] is True
            assert state["planning"]["reason"] == "required_measurement_unavailable"
            assert read_plan(tmp_path, archived) == original
            await send(session, url, config, soc=25)
            restored = await wait_ready(session, url)
            assert restored["plan"]["plan_id"] != plan["plan_id"]
            assert (
                restored["plan"]["input_snapshot"]["problem"]["batteries"][0]["battery"][
                    "initial_kwh"
                ]
                == 0.25
            )

    asyncio.run(scenario())


def test_missing_forecasts_never_use_demo_inputs_and_restart_needs_fresh_reports(tmp_path):
    """Withhold future inputs or restart live mode. Remain not ready instead of reusing demo data or old reports."""

    async def scenario():
        async with running_app(tmp_path) as (session, url):
            state = await configure(session, url)
            config = state["measurement_config"]
            state = await send(session, url, config)
            assert state["plan"] is None
            assert state["planning"]["reason"] == "waiting_for_energy_configuration"
            assert state["observations"]["measurement_version"] is not None
        async with running_app(tmp_path, FutureSource()) as (session, url):
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
            assert state["measurement_config"] == config
            assert state["plan"] is None
            assert state["observations"]["measurement_version"] is None
            assert state["planning"]["reason"] == "waiting_for_measurements"

    asyncio.run(scenario())


def test_changed_sources_revoke_commands_and_stale_config_writes_are_rejected(tmp_path):
    """Replace source selection. Revoke old commands and reject writes from an editor holding the previous revision."""

    async def scenario():
        async with running_app(tmp_path, FutureSource()) as (session, url):
            state = await configure(session, url)
            old = state["measurement_config"]
            await send(session, url, old)
            await wait_ready(session, url)
            state = await configure(session, url, old["revision"])
            assert state["plan"] is None
            assert state["measurement_config"]["revision"] != old["revision"]
            async with session.put(
                url + "/v1/sources",
                json={
                    "bindings": old["bindings"],
                    "expected_revision": old["revision"],
                },
            ) as response:
                assert response.status == 409
            bad = report_body(
                replace(configuration(), revision=old["revision"]), now=datetime.now(UTC)
            )
            async with session.post(url + "/v1/observations", json=bad) as response:
                assert response.status == 400

    asyncio.run(scenario())


def test_forecast_expiry_revokes_current_plan(tmp_path):
    """Expire the future-input basis validity. Revoke the current plan with an explicit forecast/price readiness reason."""

    async def scenario():
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            state = await configure(session, url)
            await send(session, url, state["measurement_config"])
            await wait_ready(session, url)
            future.future = replace(
                future.future, valid_until=datetime.now(UTC) - timedelta(seconds=1)
            )
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"]["reason"] == "forecast_or_prices_invalid"

    asyncio.run(scenario())


def test_new_endpoints_require_existing_installation_authentication(tmp_path):
    """Call source/observation endpoints without the installation token. Require authentication rejection."""

    async def scenario():
        async with running_app(tmp_path) as (_session, url), ClientSession() as anonymous:
            for method, path in (("PUT", "/v1/sources"), ("POST", "/v1/observations")):
                async with anonymous.request(method, url + path, json={}) as response:
                    assert response.status == 401
        assert read_state(tmp_path)["plan"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["before_cursor_execute", "after_cursor_execute"])
def test_failed_source_save_preserves_acknowledged_revision_report_plan_and_retry(
    tmp_path, monkeypatch, operation
):
    """Inject pre/post SQL update failure. Retain acknowledged state/archive content and allow retry with the original revision."""

    async def scenario():
        async with running_app(tmp_path, FutureSource()) as (session, url):
            selected = await configure(session, url)
            config = selected["measurement_config"]
            await send(session, url, config)
            original = await wait_ready(session, url)
            disk = read_state(tmp_path)
            archive = original["plan"]["plan_id"]
            archived = read_plan(tmp_path, archive)

            save = SqlPlanRepository.save

            def unavailable(repository):
                """Fail before or after the state UPDATE to exercise rollback of acknowledged configuration."""
                from sqlalchemy import event

                def fail(_connection, _cursor, statement, _parameters, _context, _many):
                    """Inject a storage failure only at the final active-state update."""
                    if statement.lstrip().upper().startswith("UPDATE OEP_ARCHIVE_STATE"):
                        raise OSError("Injected source persistence failure")

                event.listen(repository.engine, operation, fail)
                try:
                    save(repository)
                finally:
                    event.remove(repository.engine, operation, fail)

            with monkeypatch.context() as fault:
                fault.setattr(SqlPlanRepository, "save", unavailable)
                async with session.put(
                    url + "/v1/sources",
                    json={
                        "bindings": config["bindings"],
                        "expected_revision": config["revision"],
                    },
                ) as response:
                    assert response.status == 500
                async with session.get(url + "/v1/snapshot") as response:
                    retained = await response.json()
            assert retained["measurement_config"] == config
            assert retained["plan"]["plan_id"] == original["plan"]["plan_id"]
            assert (
                retained["observations"]["measurement_version"]
                == original["observations"]["measurement_version"]
            )
            assert read_state(tmp_path) == disk
            assert read_plan(tmp_path, archive) == archived
            retried = await configure(session, url, config["revision"])
            assert retried["measurement_config"]["revision"] != config["revision"]
            assert retried["plan"] is None

    asyncio.run(scenario())
