"""Independent shadow-plan economics, evidence limits and consistent model promotion."""

import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.demand.calculation.model import DemandForecast, ForecastStep
from open_energy_planner.forecast_selection.calculation.decision import learned_is_better
from open_energy_planner.forecast_selection.evaluation import score
from open_energy_planner.forecast_selection.worker import capture_pair
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.highs.replay import dispatch
from open_energy_planner.optimization.domain import Interval, Strategy
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.savings.model import Frame, PendingSettlement
from tests.app.savings.support.scenarios import START, contract, problem


@pytest.mark.parametrize("kind", ["ev_work", "appliances"])
def test_controlled_work_cannot_create_unmatched_shadow_decision_evidence(kind):
    """A shadow evaluator lacking realized controlled-load boundaries must reject comparisons, not promote a model."""
    original = {"input_snapshot": {"problem": {kind: [{"id": "controlled"}]}}}
    with pytest.raises(ValueError, match="Controlled-load realization"):
        capture_pair(original, {}, START)


def shadow_fixture():
    """Known forecasts disagree about a 1 kWh load. Charging at 0.10 avoids later purchase at 0.50."""
    tariffs = (1, 0.1, 0.5)
    inputs = problem()
    inputs = replace(
        inputs,
        pv_sources=(),
        intervals=tuple(
            Interval(START + timedelta(minutes=15 * index), 0.25, price, 0, load)
            for index, (price, load) in enumerate(zip(tariffs, (0, 1, 0), strict=True))
        ),
        batteries=(
            replace(
                inputs.batteries[0],
                battery=replace(inputs.batteries[0].battery, initial_kwh=0, capacity_kwh=1),
            ),
        ),
        terminal_valuations=(replace(inputs.terminal_valuations[0], rate=0, usable_kwh=1),),
    )
    terms = contract(buy="market", sell="0")
    snapshot = InputSnapshot(
        "original",
        "settings",
        START,
        "measurements",
        "forecast",
        "prices",
        inputs,
        contract_version=terms.version,
    )
    solved = HighsSolver().solve(inputs)
    original = plan_document(snapshot, solved, validate(inputs, solved.plan), START)
    bindings = {"terms": [{"entity_id": "sensor.load", "sign": 1}], "solar": [], "batteries": []}
    original["comparison_bindings"] = bindings
    baseline = DemandForecast(
        "demand",
        "history",
        START,
        "baseline",
        tuple(ForecastStep(row.start, 0.25, row.load_kwh) for row in inputs.intervals),
        (),
    )
    candidate = replace(
        baseline,
        model="candidate",
        steps=tuple(
            replace(row, kwh=load) for row, load in zip(baseline.steps, (0, 0, 1), strict=True)
        ),
    )
    variant = {
        "family_id": "family",
        "kind": "demand",
        "baseline": baseline,
        "candidate": candidate,
    }
    capture = capture_pair(original, variant, START)
    markets = [
        {
            "start": row.start.isoformat(),
            "end": (row.start + timedelta(minutes=15)).isoformat(),
            "price": row.buy_price,
            "version": "published",
        }
        for row in inputs.intervals[1:]
    ]
    return original, capture, terms, markets, bindings


def realized(bindings, loads):
    """Record independently specified quarter loads after the frozen decision boundary."""
    return [
        {
            "frame": Frame(
                START + timedelta(minutes=15 * (index + 1)),
                START + timedelta(minutes=15 * (index + 2)),
                {"sensor.load": load},
                {"battery": 0},
                {"battery": 0},
                {},
                "recorded",
            ).record(),
            "context": {"bindings": bindings},
        }
        for index, load in enumerate(loads)
    ]


def test_shadow_plan_financial_outcome_is_independent_of_forecast_cost():
    """Correct timing costs 0.10 instead of 0.50. The literal realized saving is 0.40."""
    original, capture, terms, markets, bindings = shadow_fixture()
    result = score(capture, original, realized(bindings, (0, 1)), markets, terms, dispatch)
    assert result["baseline"]["economic_cost"] == pytest.approx(0.5)
    assert result["candidate"]["economic_cost"] == pytest.approx(0.1)
    assert capture["start_stored"] == {"battery": 0}


def test_wrong_timing_is_not_rescued_by_hindsight_dispatch():
    """Wrong charging adds 0.10 and exports at zero. Recourse cannot revise the frozen decision."""
    original, capture, terms, markets, bindings = shadow_fixture()
    result = score(capture, original, realized(bindings, (1, 0)), markets, terms, dispatch)
    assert result["baseline"]["economic_cost"] == pytest.approx(0.1)
    assert result["candidate"]["economic_cost"] == pytest.approx(0.2)


def test_estimated_or_missing_measurements_cannot_promote_a_model():
    """Hourly interpolation or one absent quarter does not establish realized decision superiority."""
    original, capture, terms, markets, bindings = shadow_fixture()
    records = realized(bindings, (0, 1))
    with pytest.raises(PendingSettlement):
        score(capture, original, records[:1], markets, terms, dispatch)
    records[0]["frame"]["quality"] = "estimated_hourly"
    with pytest.raises(PendingSettlement):
        score(capture, original, records, markets, terms, dispatch)


def test_consistent_meaningful_financial_advantage_is_required():
    """Three 0.40 gains pass. A loss, fewer windows or a sub-cent margin cannot trigger promotion."""
    baseline = {"economic_cost": 0.5, "corrected_import_kwh": 1}
    candidate = {"economic_cost": 0.1, "corrected_import_kwh": 1}
    windows = [
        {
            "start": (START + timedelta(days=2 * index)).isoformat(),
            "end": (START + timedelta(days=2 * index + 2)).isoformat(),
            "baseline": baseline,
            "candidate": candidate,
        }
        for index in range(3)
    ]
    assert learned_is_better(windows, Strategy.COST)
    assert not learned_is_better(windows[:2], Strategy.COST)
    assert not learned_is_better(
        windows[:2] + [{**windows[2], "baseline": candidate, "candidate": baseline}], Strategy.COST
    )
    assert not learned_is_better(
        [{**row, "candidate": {**candidate, "economic_cost": 0.499}} for row in windows],
        Strategy.COST,
    )
    more_import = [
        {**row, "candidate": {**candidate, "corrected_import_kwh": 1.1}} for row in windows
    ]
    assert learned_is_better(more_import, Strategy.COST)
    assert not learned_is_better(more_import, Strategy.CONSUMPTION)
    with pytest.raises(ValueError, match="nonoverlapping"):
        learned_is_better([windows[0]] * 3, Strategy.COST)


def test_durable_preferences_restore_but_never_cross_configuration_families(tmp_path, caplog):
    """Three retained profitable windows restore preference. A changed family starts at baseline without altering plans."""
    from open_energy_planner.forecast_selection.runtime import ModelSelection
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    repository = SqlPlanRepository(tmp_path)
    try:
        documents = SqlDocuments(repository.engine, "planning")
        store = SelectionStore(documents)
        family = {"windows": [], "pending": None}
        for index in range(3):
            evaluation = {
                "start": (START + timedelta(days=index * 2)).isoformat(),
                "end": (START + timedelta(days=index * 2 + 2)).isoformat(),
                "evaluated_at": (START + timedelta(days=index * 2 + 3)).isoformat(),
                "baseline": {"economic_cost": 0.5, "corrected_import_kwh": 1},
                "candidate": {"economic_cost": 0.1, "corrected_import_kwh": 1},
            }
            family["windows"].append(version(evaluation))
            store.commit("known", family, evaluation=evaluation)
        runtime = ModelSelection(SelectionStore(documents), repository, None, None, None)
        choice = {
            "family_id": "known",
            "scope": "demand",
            "kind": "demand",
            "label": "Household consumption",
            "candidate": None,
            "baseline": None,
            "eligibility_issue": None,
        }
        original = {"strategy": "minimize cost", "currency": "EUR"}
        caplog.set_level(logging.INFO, logger="open_energy_planner.forecast_selection.runtime")
        malformed = {**choice, "family_id": "malformed"}
        del malformed["scope"]
        with pytest.raises(KeyError):
            runtime.process(original, [choice, malformed], START + timedelta(days=8))
        assert not runtime.preferences
        assert not any("Forecast model selection" in message for message in caplog.messages)
        runtime.process(original, [choice], START + timedelta(days=8))
        assert runtime.preferences == {"known": True}
        assert runtime.rows[0]["completed_windows"] == 3
        runtime.process(original, [choice], START + timedelta(days=8))
        assert sum("Forecast model selection" in message for message in caplog.messages) == 1
        runtime.process(original, [{**choice, "family_id": "changed"}], START + timedelta(days=8))
        assert runtime.preferences == {"changed": False}
        assert sum("Forecast model selection" in message for message in caplog.messages) == 2
        assert repository.state["plan"] is None
        assert len(SelectionStore(documents).state["families"]["known"]["windows"]) == 3
    finally:
        repository.close()


def test_corrupt_model_evidence_cannot_restore_a_learned_preference(tmp_path):
    """A content-hash mismatch is an explicit archive fault. Its evidence is preserved and preference stays baseline."""
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    repository = SqlPlanRepository(tmp_path)
    try:
        documents = SqlDocuments(repository.engine, "planning")
        store = SelectionStore(documents)
        capture = {"original_plan_id": "retained"}
        reference = version(capture)
        store.commit("family", {"windows": [], "pending": reference}, capture=capture)
        documents.write(f"model-capture-{reference}.json", {"original_plan_id": "corrupt"})
        restored = SelectionStore(documents)
        assert restored.issue == "model_selection_archive_unreadable"
        assert documents.read(f"model-capture-{reference}.json") == {"original_plan_id": "corrupt"}
        with pytest.raises(ValueError, match="unreadable"):
            restored.commit("family", {"windows": [], "pending": None})
    finally:
        repository.close()


def test_realized_replay_runs_in_the_bounded_native_child():
    """The real process boundary preserves the independently known 0.50/0.10 replay outcomes."""
    from open_energy_planner.forecast_selection.worker import calculate_in_child
    from open_energy_planner.optimization.adapters.process.worker import run_child

    original, capture, terms, markets, bindings = shadow_fixture()
    payload = {
        "operation": "evaluate",
        "original": original,
        "capture": capture,
        "records": realized(bindings, (0, 1)),
        "market_rows": markets,
        "contract": terms,
        "seeds": (),
    }
    result = run_child(calculate_in_child, payload, 30)
    assert result["status"] == "ok"
    assert result["value"]["baseline"]["economic_cost"] == pytest.approx(0.5)
    assert result["value"]["candidate"]["economic_cost"] == pytest.approx(0.1)
    with pytest.raises(TimeoutError):
        run_child(calculate_in_child, payload, 0.001)
    assert run_child(calculate_in_child, payload, 30)["status"] == "ok"


def test_shared_inverter_clipping_cannot_be_missed_per_source():
    """Two 0.5 kWh AC sources saturate a shared 4 kW converter for a quarter, despite neither doing so alone."""
    from open_energy_planner.forecast_selection.evaluation import realized_inputs
    from open_energy_planner.installation.model import PVUnit

    inputs = problem()
    inputs = replace(
        inputs,
        batteries=(),
        terminal_valuations=(),
        inverters=(replace(inputs.inverters[0], hybrid=False),),
        pv_sources=(PVUnit("first", "inv", False), PVUnit("second", "inv", False)),
        intervals=(
            replace(inputs.intervals[0], pv_kwh=1, pv_sources=(("first", 0.5), ("second", 0.5))),
        ),
    )
    bindings = {
        "terms": [{"entity_id": "sensor.load", "sign": 1}],
        "solar": [
            {"source_id": "first", "entity_id": "sensor.first"},
            {"source_id": "second", "entity_id": "sensor.second"},
        ],
    }
    frame = Frame(
        START,
        START + timedelta(minutes=15),
        {"sensor.load": 0.1, "sensor.first": 0.5, "sensor.second": 0.5},
        {},
        {},
        {},
        "recorded",
    )
    with pytest.raises(ValueError, match="Clipped PV"):
        realized_inputs(
            inputs,
            bindings,
            [{"frame": frame.record(), "context": {"bindings": bindings}}],
            frame.start,
            frame.end,
        )


def test_future_issued_challenger_cannot_create_historical_decision_evidence():
    """A forecast issued after capture time is rejected before it can masquerade as past-known input."""
    original, _capture, _terms, _markets, _bindings = shadow_fixture()
    baseline = DemandForecast(
        "demand",
        "history",
        START,
        "baseline",
        tuple(ForecastStep(START + timedelta(minutes=15 * index), 0.25, 0) for index in range(3)),
        (),
    )
    with pytest.raises(ValueError, match="Future forecast"):
        capture_pair(
            original,
            {
                "family_id": "future",
                "kind": "demand",
                "baseline": baseline,
                "candidate": replace(baseline, issued_at=START + timedelta(hours=1)),
            },
            START,
        )


def test_three_real_shadow_windows_promote_and_a_later_loss_restores_baseline(
    tmp_path, monkeypatch
):
    """Three independently replayed 0.40 gains promote. Reversed actual timing then loses 0.10 and restores baseline."""
    from open_energy_planner.contracts.storage import ContractStore
    from open_energy_planner.forecast_selection.runtime import ModelSelection
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.planning_policy import planning_grid
    from open_energy_planner.savings.context import physical_context
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from open_energy_planner.storage.counters import CounterStore

    original, _capture, terms, _markets, bindings = shadow_fixture()
    template = physical_context(original)
    repository = SqlPlanRepository(tmp_path)
    contracts = ContractStore(SqlDocuments(repository.engine, "contracts"))
    contracts.save(terms)
    counters = CounterStore(SqlDocuments(repository.engine, "bonus-counters"))

    class RecordedEvidence:
        """Provide only the current test window's explicitly specified actuals and published prices."""

        def frames(self, _start, *, end, limit):
            """Return completed fine records. The runtime must not request them during capture."""
            assert end == self.end and limit >= 191
            return self.records

        def published(self, _start, end, _area, _currency):
            """Return literal published prices rather than forecasts or optimized costs."""
            assert end == self.end
            return self.markets

    evidence = RecordedEvidence()
    runtime = ModelSelection(
        SelectionStore(SqlDocuments(repository.engine, "planning")),
        repository,
        evidence,
        contracts,
        counters,
    )

    def calculate(payload):
        """Run the real planning/replay implementations inline. A separate test proves spawned transport and timeout."""
        if payload["operation"] == "capture":
            value = capture_pair(payload["original"], payload["variant"], payload["now"])
        else:
            value = score(
                payload["capture"],
                payload["original"],
                payload["records"],
                payload["market_rows"],
                payload["contract"],
                dispatch,
                payload["seeds"],
            )
        return {"status": "ok", "value": value}

    monkeypatch.setattr(runtime, "_calculate", calculate)
    try:
        for window_index in range(4):
            issued = START + timedelta(days=window_index * 2)
            intervals = tuple(
                Interval(
                    start,
                    duration,
                    (1, 0.1, 0.5)[index] if index < 3 else 0.1,
                    0,
                    1 if index == 1 else 0,
                )
                for index, (start, duration) in enumerate(planning_grid(issued))
            )
            end = intervals[-1].start + timedelta(hours=intervals[-1].duration_h)
            inputs = replace(
                template,
                intervals=intervals,
                terminal_valuations=(
                    replace(
                        template.terminal_valuations[0],
                        window_start=end,
                        window_end=end + timedelta(hours=24),
                    ),
                ),
            )
            solved = HighsSolver().solve(inputs)
            snapshot = InputSnapshot(
                "original",
                "settings",
                issued,
                "measurements",
                "forecast",
                "prices",
                inputs,
                contract_version=terms.version,
            )
            original = plan_document(snapshot, solved, validate(inputs, solved.plan), issued)
            original.update(comparison_bindings=bindings, market_area="NL", currency="EUR")
            repository.state["plan"] = original
            repository.save()
            baseline = DemandForecast(
                "demand",
                "history",
                issued,
                "baseline",
                tuple(ForecastStep(row.start, row.duration_h, row.load_kwh) for row in intervals),
                (),
            )
            candidate = replace(
                baseline,
                model="candidate",
                steps=tuple(
                    replace(row, kwh=1 if index == 2 else 0)
                    for index, row in enumerate(baseline.steps)
                ),
            )
            choice = {
                "family_id": "family",
                "scope": "demand",
                "kind": "demand",
                "label": "Consumption",
                "eligibility_issue": None,
                "baseline": baseline,
                "candidate": candidate,
            }
            runtime.process(original, [choice], issued)
            assert runtime.store.state["families"]["family"]["pending"] is not None
            # Only after the pair is retained do its actual load and price evidence become available.
            evidence.end = end
            evidence.records = []
            for quarter_index in range(1, 192):
                start = issued + timedelta(minutes=15 * quarter_index)
                actual_load = 1 if quarter_index == (2 if window_index < 3 else 1) else 0
                frame = Frame(
                    start,
                    start + timedelta(minutes=15),
                    {"sensor.load": actual_load},
                    {"battery": 0},
                    {"battery": 0},
                    {},
                    "recorded",
                )
                evidence.records.append(
                    {"frame": frame.record(), "context": {"bindings": bindings}}
                )
            evidence.markets = [
                {
                    "start": row.start.isoformat(),
                    "end": (row.start + timedelta(hours=row.duration_h)).isoformat(),
                    "price": row.buy_price,
                    "version": "published",
                }
                for row in intervals
            ]
            runtime.process(original, [choice], end)
            assert runtime.preferences["family"] is (window_index == 2)
            assert runtime.rows[0]["windows"][-1]["baseline"]["economic_cost"] == pytest.approx(
                0.5 if window_index < 3 else 0.1
            )
            assert runtime.rows[0]["windows"][-1]["candidate"]["economic_cost"] == pytest.approx(
                0.1 if window_index < 3 else 0.2
            )
            assert repository.get_plan(original["plan_id"])["steps"] == original["steps"]
        assert len(runtime.store.state["families"]["family"]["windows"]) == 3
    finally:
        repository.close()


def test_frozen_choices_use_independent_measured_bonus_budgets():
    """Only 0.2 kWh remains at 0.10/kWh: export earns 0.02 while the idle choice consumes no budget."""
    from open_energy_planner.contracts.bonus import CounterSeed

    original, capture, _terms, markets, bindings = shadow_fixture()
    terms = contract(bonus=True, buy="market", sell="0")
    records = realized(bindings, (1, 0))
    for record in records:
        record["frame"]["meter_start"] = {"sensor.export": 100}
    seed = CounterSeed(
        "bonus", terms.bonuses[0].period_start(START), 1.8, 100, "sensor.export", START
    )
    result = score(capture, original, records, markets, terms, dispatch, (seed,))
    assert result["baseline"]["bonus"] == 0
    assert result["candidate"]["bonus"] == pytest.approx(0.02)
    assert result["candidate"]["economic_cost"] == pytest.approx(0.18)
    assert list(result["baseline"]["counters"].values()) == pytest.approx([1.8])
    assert list(result["candidate"]["counters"].values()) == pytest.approx([2.8])


def test_billing_edges_keep_identical_frozen_commands_replayable():
    """A daylight/rate edge inside a quarter cannot hide complete metering when the physical command stays equal."""
    from copy import deepcopy

    from open_energy_planner.forecast_selection.evaluation import frozen_commands

    original, capture, terms, markets, bindings = shadow_fixture()
    modified = deepcopy(capture)
    for scenario in ("baseline", "candidate"):
        first = modified[scenario]["steps"][0]
        edge = (START + timedelta(minutes=22)).isoformat()
        modified[scenario]["steps"] = [
            {**first, "end": edge},
            {**first, "start": edge},
            *modified[scenario]["steps"][1:],
        ]
    result = score(modified, original, realized(bindings, (0, 1)), markets, terms, dispatch)
    assert result["baseline"]["economic_cost"] == pytest.approx(0.5)
    assert result["candidate"]["economic_cost"] == pytest.approx(0.1)
    changed = deepcopy(modified["candidate"]["steps"])
    changed[1]["devices"] = deepcopy(changed[1]["devices"])
    changed[1]["devices"]["battery"]["power_w"] = 0
    with pytest.raises(ValueError, match="finer evidence"):
        frozen_commands(changed, START + timedelta(minutes=15), START + timedelta(minutes=30))


def test_model_family_identity_tracks_location_contract_strategy_and_feature_mode(tmp_path):
    """Changed physical/financial/site definitions cannot reuse old preferences. Ridge feature modes are isolated."""
    from open_energy_planner.demand.runtime import DemandRuntime
    from open_energy_planner.demand.storage import DemandStore
    from open_energy_planner.forecast_selection.runtime import variants
    from open_energy_planner.installation.model import Installation
    from open_energy_planner.solar.calculation.model import SolarForecast
    from open_energy_planner.solar.learning import Calibration
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from tests.app.demand.test_demand import config
    from tests.app.solar.test_solar import SITE, plant

    repository = SqlPlanRepository(tmp_path)
    try:
        demand = DemandRuntime(DemandStore(SqlDocuments(repository.engine, "demand")))
        demand.configure(config())
        demand.baseline = DemandForecast(
            demand.configuration.version, "history", START, "weekday-hour-v2", (), ()
        )
        demand.candidate = replace(demand.baseline, model="ridge-v2")
        solar = SolarRuntime(SolarStore(SqlDocuments(repository.engine, "solar")))
        solar.configure(plant(count=2))
        solar.location = SITE
        solar.baseline = SolarForecast(solar.configuration.version, SITE, START, "weather", ())
        solar.candidate = replace(solar.baseline, model="pvwatts-calibrated-v2")
        solar.calibrations = tuple(
            Calibration(source.source_id, 1.1, 100, "training", START)
            for source in solar.configuration.sources
        )
        installation = Installation("sensor.grid", 10000, 10000)
        terms = contract()
        first = variants(installation, demand, solar, terms, Strategy.COST, "counter-a")
        assert len({row["family_id"] for row in first}) == 3
        first_ids = {row["family_id"] for row in first}
        assert first_ids.isdisjoint(
            row["family_id"]
            for row in variants(
                installation, demand, solar, terms, Strategy.CONSUMPTION, "counter-a"
            )
        )
        assert first_ids.isdisjoint(
            row["family_id"]
            for row in variants(installation, demand, solar, terms, Strategy.COST, "counter-b")
        )
        solar.location = replace(SITE, latitude=53)
        assert first_ids.isdisjoint(
            row["family_id"]
            for row in variants(installation, demand, solar, terms, Strategy.COST, "counter-a")
        )
        solar.location = SITE
        demand.candidate_features = "temperature"
        temperature = variants(installation, demand, solar, terms, Strategy.COST, "counter-a")
        assert first[0]["family_id"] != temperature[0]["family_id"]
        assert [row["family_id"] for row in first[1:]] == [
            row["family_id"] for row in temperature[1:]
        ]
    finally:
        repository.close()


def unresponsive_selection_child(payload, sender):
    """Deliberately withhold a result while the parent proves it can stop a running native child."""
    import time

    assert payload == "wait"
    time.sleep(60)
    sender.close()


def test_selection_child_can_be_stopped_without_waiting_for_workload_budget():
    """App shutdown reaps a running child through its stop event instead of waiting for a long evaluation deadline."""
    import multiprocessing
    from threading import Event, Timer

    from open_energy_planner.optimization.adapters.process.worker import run_child

    children = {child.pid for child in multiprocessing.active_children()}
    stop = Event()
    timer = Timer(0.05, stop.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="worker_cancelled"):
            run_child(unresponsive_selection_child, "wait", 2, cancel=stop)
    finally:
        timer.cancel()
        timer.join()
    assert {child.pid for child in multiprocessing.active_children()} == children


def test_actual_capture_window_minimum_is_checked_before_launch(tmp_path, monkeypatch):
    """A plan ending 47.5 hours from now leaves only 47.25 hours after the next quarter, so no capture is launched."""
    from open_energy_planner.forecast_selection.runtime import ModelSelection
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    repository = SqlPlanRepository(tmp_path)
    try:
        runtime = ModelSelection(
            SelectionStore(SqlDocuments(repository.engine, "planning")),
            repository,
            None,
            None,
            None,
        )

        def forbidden(_payload):
            """An undersized window must be rejected before any numerical worker or evidence collection."""
            raise AssertionError("Short comparison reached the worker")

        monkeypatch.setattr(runtime, "_calculate", forbidden)
        original = {
            "valid_until": (START + timedelta(minutes=15)).isoformat(),
            "input_snapshot": {
                "problem": {
                    "intervals": [
                        {
                            "start": (START + timedelta(hours=47, minutes=15)).isoformat(),
                            "duration_h": 0.25,
                        }
                    ]
                }
            },
        }
        family = {"windows": [], "pending": None}
        assert runtime._advance(original, {"family_id": "short"}, family, START)
        assert runtime.store.state["families"]["short"]["pending"] is None
        assert "full-horizon" in runtime.store.state["families"]["short"]["issue"]
    finally:
        repository.close()


def test_model_evaluation_timeout_preserves_the_pending_capture(tmp_path, monkeypatch):
    """A timed-out evaluation keeps its immutable pair and retries. It never becomes a zero score or a discarded window."""
    from open_energy_planner.contracts.storage import ContractStore
    from open_energy_planner.forecast_selection import runtime as selection_runtime
    from open_energy_planner.forecast_selection.runtime import ModelSelection
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from open_energy_planner.storage.counters import CounterStore

    original, capture, terms, markets, bindings = shadow_fixture()
    original.update(market_area="NL", currency="EUR")
    repository = SqlPlanRepository(tmp_path)
    try:
        repository.state["plan"] = original
        repository.save()
        contracts = ContractStore(SqlDocuments(repository.engine, "contracts"))
        contracts.save(terms)
        counters = CounterStore(SqlDocuments(repository.engine, "bonus-counters"))
        store = SelectionStore(SqlDocuments(repository.engine, "planning"))
        reference = version(capture)
        family = {"windows": [], "pending": reference}
        store.commit("family", family, capture=capture)

        class Evidence:
            """Offer complete literal actuals. Only the numerical operation is made to time out."""

            def frames(self, _start, *, end, limit):
                """Return genuine fine evidence without altering the retained command pair."""
                return realized(bindings, (0, 1))

            def published(self, _start, _end, _area, _currency):
                """Return known complete prices, isolating timeout from absent market data."""
                return markets

        runtime = ModelSelection(store, repository, Evidence(), contracts, counters)

        def timeout(*_args, **_kwargs):
            """Simulate the parent numerical deadline, not a missing-price or storage error."""
            raise TimeoutError("fixture deadline")

        monkeypatch.setattr(selection_runtime, "run_child", timeout)
        end = START + timedelta(minutes=45)
        runtime._advance(original, {"family_id": "family"}, family, end)
        restored = SelectionStore(SqlDocuments(repository.engine, "planning"))
        assert restored.state["families"]["family"]["pending"] == reference
        assert restored.state["families"]["family"]["windows"] == []
        assert "timed out" in restored.state["families"]["family"]["issue"]
        assert restored.read("capture", reference) == capture
    finally:
        repository.close()


def test_short_retained_windows_have_a_distinct_duration_error():
    """Three disjoint one-day records are not near-horizon evidence, even when their literal financial gains are positive."""
    windows = [
        {
            "start": (START + timedelta(days=2 * index)).isoformat(),
            "end": (START + timedelta(days=2 * index + 1)).isoformat(),
            "baseline": {"economic_cost": 0.5, "corrected_import_kwh": 1},
            "candidate": {"economic_cost": 0.1, "corrected_import_kwh": 1},
        }
        for index in range(3)
    ]
    with pytest.raises(ValueError, match="shorter than the near-horizon minimum"):
        learned_is_better(windows, Strategy.COST)


def test_child_replay_timeout_is_pending_evidence(monkeypatch):
    """A per-frame native timeout crosses the worker boundary as pending, never invalid evidence."""
    from open_energy_planner.forecast_selection import worker
    from open_energy_planner.optimization.adapters.highs import replay

    original, capture, terms, markets, bindings = shadow_fixture()

    def timeout(*_args):
        """Represent an exhausted native frame with otherwise complete recorded evidence."""
        raise TimeoutError("Replay native time limit")

    class Sender:
        """Record the exact child-process transport result without starting another process."""

        def send(self, result):
            """Retain the worker's result for the assertion."""
            self.result = result

        def close(self):
            """Mirror the pipe close contract without external resources."""

    sender = Sender()
    monkeypatch.setattr(replay, "dispatch", timeout)
    worker.calculate_in_child(
        {
            "operation": "evaluate",
            "original": original,
            "capture": capture,
            "records": realized(bindings, (0, 1)),
            "market_rows": markets,
            "contract": terms,
            "seeds": (),
        },
        sender,
    )
    assert sender.result == {"status": "pending", "issue": "Replay native time limit"}
