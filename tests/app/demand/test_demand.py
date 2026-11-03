"""Independent household balances, fallback forecasts and chronological model decisions."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandHour,
    EnergyTerm,
    ExcludedLoad,
    TemperatureHour,
)
from open_energy_planner.demand.evaluation import compare
from open_energy_planner.demand.reconstruction import DemandReconstructor
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from tests.database.support.archive import corrupt_document, make_store, read_document

START = datetime(2026, 1, 1, tzinfo=UTC)


def config():
    """Define one directly measured household AC channel and a 3650 kWh/year startup estimate."""
    return DemandConfiguration("Europe/Amsterdam", 3650, (EnergyTerm("sensor.house", 1),))


def channel(entity, deltas):
    """Create a statistics anchor and independently supplied hourly increments."""
    rows, total = [EnergyHour(START - timedelta(hours=1), 100, 100)], 100
    for index, delta in enumerate(deltas):
        total += delta
        rows.append(EnergyHour(START + timedelta(hours=index), total, total))
    return EnergyHistory(entity, tuple(rows))


def samples(days=70, *, trend=False):
    """Provide a known 0.4 kWh/h household or a slowly rising daily level, without any excluded load."""
    return tuple(
        DemandHour(
            START + timedelta(hours=index_item),
            0.4 + (index_item // 24) * 0.02 if trend else 0.4,
            0.4 + (index_item // 24) * 0.02 if trend else 0.4,
        )
        for index_item in range(days * 24)
    )


@pytest.mark.parametrize("topology", ["no_battery", "separate_ac", "hybrid_ac"])
def test_known_ac_household_balance_and_measured_exclusion(topology):
    """Net 3-1 plus AC supply 3 gives 5 kWh home demand. Measured EV 2 leaves residual 3, independent of inverter arrangement."""
    values = {"sensor.import": 3, "sensor.export": 1, "sensor.ev": 2}
    terms = [EnergyTerm("sensor.import", 1), EnergyTerm("sensor.export", -1)]
    if topology == "separate_ac":
        values.update({"sensor.pv": 4, "sensor.discharge": 1, "sensor.charge": 2})
        terms.extend(
            (
                EnergyTerm("sensor.pv", 1),
                EnergyTerm("sensor.discharge", 1),
                EnergyTerm("sensor.charge", -1),
            )
        )
    else:
        supply = "sensor.hybrid_ac" if topology == "hybrid_ac" else "sensor.pv"
        values[supply] = 3
        terms.append(EnergyTerm(supply, 1))
    definition = replace(
        config(), terms=tuple(terms), exclusions=(ExcludedLoad("ev", "sensor.ev"),)
    )
    histories = tuple(channel(entity, [value]) for entity, value in values.items())
    result = DemandReconstructor().reconstruct(
        definition, histories, before=START + timedelta(hours=1)
    )
    assert result == (DemandHour(START, 5, 3),)


def test_missing_hours_and_exclusion_never_become_zero():
    """A missing cumulative sample invalidates both adjacent differences. Missing EV history cannot masquerade as no EV use."""
    data = channel("sensor.house", [1, 2, 3])
    broken = replace(data, hours=(data.hours[0], data.hours[1], data.hours[3]))
    result = DemandReconstructor().reconstruct(
        config(), (broken,), before=START + timedelta(hours=3)
    )
    assert result == (DemandHour(START, 1, 1),)
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    assert (
        DemandReconstructor().reconstruct(definition, (data,), before=START + timedelta(hours=3))
        == ()
    )


def test_impossible_subtraction_and_duplicate_channels_are_rejected():
    """Excluding 2 kWh from a 1 kWh house is inconsistent. The same source cannot be counted twice."""
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    with pytest.raises(ValueError, match="Inconsistent"):
        DemandReconstructor().reconstruct(
            definition,
            (channel("sensor.house", [1]), channel("sensor.ev", [2])),
            before=START + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="Duplicated"):
        replace(config(), terms=(EnergyTerm("sensor.house", 1), EnergyTerm("sensor.house", -1)))


@pytest.mark.parametrize("corrected_entity", ["sensor.house", "sensor.ev"])
@pytest.mark.parametrize("correction_after_gap", [False, True])
def test_counter_correction_withholds_both_adjacent_differences(
    corrected_entity, correction_after_gap
):
    """A later decrease invalidates the preceding spike even across a gap. Other complete hours retain their measured balance."""
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    histories = {
        "sensor.house": channel("sensor.house", [1, 1, 1, 1]),
        "sensor.ev": channel("sensor.ev", [0.2, 0.2, 0.2, 0.2]),
    }
    corrected = histories[corrected_entity]
    histories[corrected_entity] = replace(
        corrected,
        hours=tuple(
            replace(hour, total_kwh=hour.total_kwh + 10) if index == 2 else hour
            for index, hour in enumerate(corrected.hours)
            if not (correction_after_gap and index == 3)
        ),
    )
    reconstructed = DemandReconstructor().reconstruct(
        definition, tuple(histories.values()), before=START + timedelta(hours=4)
    )
    expected_starts = [START] if correction_after_gap else [START, START + timedelta(hours=3)]
    assert [hour.start for hour in reconstructed] == expected_starts
    assert [hour.total_kwh for hour in reconstructed] == [1] * len(expected_starts)
    assert [hour.residual_kwh for hour in reconstructed] == pytest.approx(
        [0.8] * len(expected_starts)
    )


def test_meter_reset_uses_statistics_sum_without_negative_household_load():
    """A physical meter falls from 100 to 1 while its normalized sum increases 2. Demand stays 2 kWh."""
    data = channel("sensor.house", [2])
    data = replace(data, hours=(data.hours[0], replace(data.hours[1], meter_kwh=1)))
    assert (
        DemandReconstructor()
        .reconstruct(config(), (data,), before=START + timedelta(hours=1))[0]
        .residual_kwh
        == 2
    )


@pytest.mark.parametrize("negative_kwh", [0.001, 0.002, 0.002001])
@pytest.mark.parametrize("boundary", ["household", "residual"])
def test_rounding_budget_and_rejection_are_per_complete_hour(negative_kwh, boundary):
    """Two meters allow 2 Wh of rounding, not 2.001 Wh. Neighboring valid hours remain measured."""
    if boundary == "household":
        definition = replace(
            config(), terms=(EnergyTerm("sensor.house", 1), EnergyTerm("sensor.export", -1))
        )
        histories = (
            channel("sensor.house", [1, 1, 1]),
            channel("sensor.export", [0.2, 1 + negative_kwh, 0.2]),
        )
    else:
        definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
        histories = (
            channel("sensor.house", [1, 1, 1]),
            channel("sensor.ev", [0.2, 1 + negative_kwh, 0.2]),
        )
    original = repr(histories)
    result = DemandReconstructor().analyze(definition, histories, before=START + timedelta(hours=3))
    assert repr(histories) == original
    assert result.samples[0].residual_kwh == pytest.approx(0.8)
    assert result.samples[-1].residual_kwh == pytest.approx(0.8)
    if negative_kwh <= 0.002:
        assert len(result.samples) == 3
        assert result.samples[1].residual_kwh == 0
        assert result.samples[1].total_kwh == (0 if boundary == "household" else 1)
        assert result.document() == {"usable_hours": 3, "corrected_hours": 1, "rejected_hours": 0}
    else:
        assert [hour.start for hour in result.samples] == [START, START + timedelta(hours=2)]
        assert result.document() == {"usable_hours": 2, "corrected_hours": 0, "rejected_hours": 1}
    assert (
        DemandReconstructor().reconstruct(definition, histories, before=START + timedelta(hours=3))
        == result.samples
    )


def test_household_budget_cannot_borrow_rounding_allowance_from_exclusions():
    """A 2.5 Wh negative two-meter household balance is rejected even with a third exclusion meter."""
    definition = replace(
        config(),
        terms=(EnergyTerm("sensor.house", 1), EnergyTerm("sensor.export", -1)),
        exclusions=(ExcludedLoad("ev", "sensor.ev"),),
    )
    result = DemandReconstructor().analyze(
        definition,
        (
            channel("sensor.house", [1]),
            channel("sensor.export", [1.0025]),
            channel("sensor.ev", [0]),
        ),
        before=START + timedelta(hours=1),
    )
    assert result.samples == ()
    assert result.rejected_hours == 1


def test_small_decreasing_source_sum_is_not_treated_as_balance_rounding():
    """Even a 0.1 Wh source decrease invalidates both adjacent differences rather than learning zeros."""
    history = channel("sensor.house", [1, -0.0001, 1])
    result = DemandReconstructor().analyze(config(), (history,), before=START + timedelta(hours=3))
    assert result.samples == (DemandHour(START + timedelta(hours=2), 1, 1),)
    assert result.corrected_hours == result.rejected_hours == 0


@pytest.mark.parametrize("rejected_hours", [0, 1, 23, 24])
@pytest.mark.parametrize("history_hours", [24, 168, 169, 240])
def test_runtime_retains_usable_training_and_reports_substantial_loss(
    tmp_path, rejected_hours, history_hours
):
    """One bad hour cannot erase ten days. A day of rejected balances or insufficient remainder stays actionable."""
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    store = make_store(tmp_path, DemandStore)
    store.save(definition)
    runtime = DemandRuntime(store)
    runtime.histories = {
        "sensor.house": channel("sensor.house", [1] * history_hours),
        "sensor.ev": channel(
            "sensor.ev", [2] * rejected_hours + [0.2] * (history_hours - rejected_hours)
        ),
    }
    now = START + timedelta(hours=history_hours)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == (
        "weekday-hour-v2" if history_hours - rejected_hours >= 168 else "startup-v2"
    )
    assert runtime.issue == (
        "demand_boundary_inconsistent"
        if rejected_hours and (rejected_hours >= 24 or history_hours - rejected_hours < 168)
        else None
    )
    assert runtime.document(now)["history_quality"] == {
        "usable_hours": history_hours - rejected_hours,
        "corrected_hours": 0,
        "rejected_hours": rejected_hours,
    }
    old_forecast = runtime.forecast
    old_archive = store.documents.read("forecast-" + old_forecast.version + ".json")
    runtime.histories["sensor.ev"] = channel("sensor.ev", [0.2] * history_hours)
    runtime.data_versions["sensor.ev"] = "recovered"
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.issue is None
    assert runtime.history_quality["rejected_hours"] == 0
    assert store.documents.read("forecast-" + old_forecast.version + ".json") == old_archive
    runtime.configure(definition)
    assert runtime.history_quality is None


def test_rounding_noise_does_not_raise_a_runtime_problem(tmp_path):
    """Repeated 1 Wh residual noise becomes training-only zeros without a persistent fault."""
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    store = make_store(tmp_path, DemandStore)
    store.save(definition)
    runtime = DemandRuntime(store)
    runtime.histories = {
        "sensor.house": channel("sensor.house", [1] * 240),
        "sensor.ev": channel("sensor.ev", [1.001] * 240),
    }
    now = START + timedelta(hours=240)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.issue is None
    assert runtime.forecast.model == "weekday-hour-v2"
    assert runtime.history_quality == {
        "usable_hours": 240,
        "corrected_hours": 240,
        "rejected_hours": 0,
    }


def test_no_history_always_has_startup_prediction_on_the_120_step_horizon():
    """3650 kWh/year gives 10 kWh per normal day, split into 96 quarters and 24 hours over 48 hours."""
    forecast = predict(config(), (), START, WeekdayProfile.fit(config(), ()))
    assert forecast.model == "startup-v2"
    assert forecast.quality == "provisional"
    assert len(forecast.steps) == 120
    assert all(step.duration_h == 0.25 for step in forecast.steps[:96])
    assert all(step.duration_h == 1 for step in forecast.steps[96:])
    assert sum(step.kwh for step in forecast.steps[:96]) == pytest.approx(10)
    assert sum(step.kwh for step in forecast.steps[96:]) == pytest.approx(10)


@pytest.mark.parametrize(
    "start", [datetime(2026, 3, 29, tzinfo=UTC), datetime(2026, 10, 25, tzinfo=UTC)]
)
def test_horizon_remains_48_absolute_hours_across_dst(start):
    """Local clock changes alter feature hours, never the elapsed 48-hour horizon or interval continuity."""
    forecast = predict(config(), (), start, WeekdayProfile.fit(config(), ()))
    assert forecast.steps[-1].start + timedelta(hours=1) == start + timedelta(hours=48)
    for left, right in zip(forecast.steps, forecast.steps[1:]):
        assert left.start + timedelta(hours=left.duration_h) == right.start


@pytest.mark.parametrize("training_days", [27, 28])
def test_ridge_requires_twenty_eight_actual_days(training_days):
    """A setup estimate cannot substitute for the ridge model's minimum 672 real hourly samples."""
    if training_days == 27:
        with pytest.raises(ValueError, match="Insufficient history"):
            RidgeDemandModel.fit(config(), samples(training_days))
    else:
        model = RidgeDemandModel.fit(config(), samples(training_days))
        assert model.trained_until == START + timedelta(days=28)


def test_learned_profile_and_ridge_recover_constant_residual_demand():
    """Constant measured residual 0.4 kWh/h gives 0.1 kWh/quarter for both models. No invented offset is needed."""
    data = samples(35)
    now = data[-1].start + timedelta(hours=1)
    for model in (WeekdayProfile.fit(config(), data), RidgeDemandModel.fit(config(), data)):
        forecast = predict(config(), data, now, model)
        assert forecast.steps[0].kwh == pytest.approx(0.1, abs=1e-8)
        assert sum(step_item.kwh for step_item in forecast.steps) == pytest.approx(19.2, abs=1e-6)


def test_temperature_features_learn_heating_without_future_weather_leakage():
    """Fit known heating demand. Colder forecasts increase demand, while weather received after issue is unavailable."""
    data, weather = [], []
    for index_item in range(35 * 24):
        instant = START + timedelta(hours=index_item)
        temperature = -5 + (index_item // 24) % 30
        energy = 0.3 + 0.05 * max(0, 18 - temperature)
        data.append(DemandHour(instant, energy, energy))
        weather.append(TemperatureHour(instant, temperature, instant + timedelta(hours=1)))
    model = RidgeDemandModel.fit(config(), tuple(data), temperatures=tuple(weather), alpha=0.1)
    assert model.temperature_enabled
    now = START + timedelta(days=35)
    assert model.predict(now, temperature=-5) > model.predict(now, temperature=20) + 0.5
    future = tuple(
        TemperatureHour(now + timedelta(hours=index_item_entry), 5, now + timedelta(minutes=1))
        for index_item_entry in range(49)
    )
    with pytest.raises(ValueError, match="coverage"):
        predict(config(), tuple(data), now, model, temperatures=future)


class PlanningScorer:
    """Plan identical two-hour battery cases and settle against actual load, with fixed prices and limits."""

    def cost(self, forecast, actual):
        """Charge at 0.1 to cover the predicted 1.0-price hour. Settle actual shortage and leftover energy independently."""
        from open_energy_planner.installation.validation import validate
        from open_energy_planner.installation.valuation import TerminalValuation
        from open_energy_planner.optimization.adapters.highs import HighsSolver
        from open_energy_planner.optimization.domain import Battery, Interval
        from tests.app.installation.support.problems import installation_problem

        predicted = sum(step.kwh for step in forecast.steps[4:8])
        problem = installation_problem(
            (
                Interval(forecast.issued_at, 1, 0.1, 0, load_kwh=0),
                Interval(forecast.issued_at + timedelta(hours=1), 1, 1, 0, load_kwh=predicted),
            ),
            battery=Battery(10, 0, charge_limit_w=10000, discharge_limit_w=10000),
            grid_import_limit_w=100000,
            grid_export_limit_w=4000,
        )
        end = forecast.issued_at + timedelta(hours=2)
        problem = replace(
            problem,
            terminal_valuations=(
                TerminalValuation(
                    "battery", 0, 10, 0.05, end, end + timedelta(hours=24), "trailing_window_proxy"
                ),
            ),
        )
        plan = HighsSolver().solve(problem).plan
        validate(problem, plan)
        charge = plan.flows[0].batteries[0].charge_kwh
        realized = actual[1].residual_kwh
        return (
            0.1 * (actual[0].residual_kwh + charge)
            + max(0, realized - charge)
            - 0.05 * max(0, charge - realized)
        )


def test_chronological_real_planning_comparison_requires_consistent_financial_gain():
    """A steadily rising load makes the old weekday mean undercharge. Ridge must reduce actual expensive-hour purchases in all three windows."""
    data = samples(70, trend=True)
    cutoffs = tuple(START + timedelta(days=day) for day in (60, 62, 64))
    result = compare(config(), data, cutoffs, PlanningScorer())
    assert result.improved
    assert all(
        old > new for old, new in zip(result.baseline_costs, result.ridge_costs, strict=True)
    )
    unchanged = compare(config(), samples(70), cutoffs, PlanningScorer())
    assert not unchanged.improved
    with pytest.raises(ValueError, match="disjoint"):
        compare(config(), data, (cutoffs[0], cutoffs[0], cutoffs[2]), PlanningScorer())


def test_runtime_always_forecasts_and_keeps_unvalidated_ridge_in_shadow(tmp_path):
    """Startup works without history. Sufficient data produces a Ridge candidate without authorizing its use from accuracy alone."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    now = START + timedelta(days=70)
    result = runtime.build(runtime.job(now))
    assert runtime.install(result, now)
    assert runtime.forecast.model == "startup-v2"
    runtime.histories["sensor.house"] = channel("sensor.house", [0.4] * (70 * 24))
    runtime.data_versions["sensor.house"] = "new-history"
    result = runtime.build(runtime.job(now))
    assert runtime.install(result, now)
    assert runtime.forecast.model == "weekday-hour-v2"
    assert runtime.candidate.model == "ridge-v2"
    assert runtime.model_status == "shadow_waiting_for_decision_evidence"
    runtime.configure(replace(config(), annual_kwh=4000))
    assert not runtime.install(result, now)
    assert not runtime.failed(result[0], now)
    assert runtime.failed(runtime.job(now)[0], now)
    assert runtime.job(now + timedelta(minutes=1)) is None
    assert runtime.job(now + timedelta(minutes=6)) is not None


def test_sparse_night_history_does_not_turn_unknown_daytime_into_zero_demand():
    """A single measured zero night hour is insufficient for a whole-day learned profile. Annual startup remains active."""
    data = (DemandHour(START, 0, 0),)
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    forecast = predict(
        definition, data, START + timedelta(days=1), WeekdayProfile.fit(definition, data)
    )
    assert forecast.model == "startup-v2"
    assert forecast.scope == "total_household"
    assert forecast.excluded_devices == ()
    assert sum(step.kwh for step in forecast.steps) > 0


def test_ridge_promotion_uses_consistent_realized_solver_decisions(tmp_path):
    """A winning Ridge model is promoted and its JSON parameters reproduce every original prediction."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store, scorer=PlanningScorer())
    runtime.histories["sensor.house"] = channel(
        "sensor.house", [source_item.residual_kwh for source_item in samples(70, trend=True)]
    )
    runtime.data_versions["sensor.house"] = "measured-trend"
    now = START + timedelta(days=70)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.model_status == "active"
    assert runtime.forecast.model == "ridge-v2"
    assert runtime.comparison.improved
    archive = f"forecast-{runtime.forecast.version}.json"
    parameters = json.loads(read_document(tmp_path, "demand", archive))["model_parameters"]
    restored = RidgeDemandModel(
        timezone=parameters["timezone"],
        coefficients=tuple(parameters["coefficients"]),
        means=tuple(parameters["means"]),
        scales=tuple(parameters["scales"]),
        temperature_enabled=parameters["temperature_enabled"],
        trained_until=datetime.fromisoformat(parameters["trained_until"]),
        recent_kwh=parameters["recent_kwh"],
    )
    assert predict(config(), (), now, restored).steps == runtime.forecast.steps


def test_source_histories_commit_as_one_cohort_and_stale_requests_cannot_change_it(tmp_path):
    """Receive household and EV history sequentially. Only the complete matching range becomes training input."""
    store = make_store(tmp_path, DemandStore)
    definition = replace(config(), exclusions=(ExcludedLoad("ev", "sensor.ev"),))
    store.save(definition)
    runtime = DemandRuntime(store)
    now = START + timedelta(days=3)
    first = runtime.request(now)
    runtime.receive(first["version"], channel("sensor.house", [1, 1]), now)
    assert not runtime.histories
    second = runtime.request(now)
    assert second["entity_id"] == "sensor.ev"
    with pytest.raises(ValueError, match="changed"):
        runtime.receive(first["version"], channel("sensor.house", [1, 1]), now)
    runtime.receive(second["version"], channel("sensor.ev", [0.2, 0.2]), now)
    assert set(runtime.histories) == {"sensor.house", "sensor.ev"}
    assert runtime.request(now) is None
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.history_quality == {"usable_hours": 2, "corrected_hours": 0, "rejected_hours": 0}
    previous_quality = runtime.history_quality
    later = now + timedelta(days=1)
    first = runtime.request(later)
    runtime.receive(first["version"], channel("sensor.house", [1, 1, 1]), later)
    assert runtime.history_quality == previous_quality
    second = runtime.request(later)
    runtime.receive(second["version"], channel("sensor.ev", [0.2, 0.2, 0.2]), later)
    assert runtime.document(later)["history_quality"] is None
    job = runtime.job(later)
    assert runtime.failed(job[0], later)
    assert runtime.document(later)["history_quality"] is None
    assert runtime.install(runtime.build(job), later)
    assert runtime.history_quality == {"usable_hours": 3, "corrected_hours": 0, "rejected_hours": 0}


@pytest.mark.parametrize("minute,tail", [(0, 1), (15, 0.25), (30, 0.5), (45, 0.75)])
def test_forecast_projection_uses_the_model_contract_without_concrete_type_checks(minute, tail):
    """An interchangeable 1-kWh/hour predictor keeps exactly 48 kWh for every clock offset, including clipped 30/45-minute tails."""

    @dataclass(frozen=True)
    class AlternativeModel:
        """Represent another local forecasting adapter through the shared narrow protocol."""

        model_id: str = "alternative-test-v1"
        residual_profile: bool = True

        def predict(self, instant, *, temperature=None, recent=None):
            """Return the independently specified 1 kWh/hour rate."""
            return 1.0

    start = START.replace(minute=minute)
    forecast = predict(config(), (), start, AlternativeModel())
    assert forecast.steps[-1].duration_h == tail
    assert forecast.steps[-1].kwh == tail
    assert forecast.steps[-1].start + timedelta(hours=tail) == start + timedelta(hours=48)
    assert forecast.model == "alternative-test-v1"
    assert forecast.steps[0].kwh == 0.25
    assert sum(step.kwh for step in forecast.steps) == 48


def test_unreadable_demand_configuration_cannot_silently_restore_upstream_load(tmp_path):
    """A corrupt saved demand pointer blocks current installation planning rather than replacing household load."""
    from types import SimpleNamespace

    from open_energy_planner.home_assistant.observations import InputsUnavailable
    from open_energy_planner.solar.calculation.model import SolarConfiguration
    from tests.app.forecast.support.basis import forecast_basis

    store = make_store(tmp_path, DemandStore)
    store.save(config())
    corrupt_document(tmp_path, "demand", "current.json", '"invalid-reference"')
    restored = make_store(tmp_path, DemandStore)
    assert restored.issue == "demand_configuration_unreadable"
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=SolarConfiguration((), ()), forecast=None
    )
    source, configuration, report = forecast_basis(START, solar, demand=DemandRuntime(restored))
    with pytest.raises(InputsUnavailable, match="demand_forecast_pending"):
        source.current(configuration, START, report=report)


@pytest.mark.parametrize("rejected_hours", [24, 240])
def test_training_quality_warning_preserves_honest_household_planning(tmp_path, rejected_hours):
    """Plan from retained 0.8 kWh hours or a declared 10 kWh/day startup estimate, never fabricated zeros."""
    from types import SimpleNamespace

    from open_energy_planner.solar.calculation.model import SolarConfiguration
    from tests.app.forecast.support.basis import forecast_basis

    definition = replace(
        config(), terms=(EnergyTerm("sensor.house", 1), EnergyTerm("sensor.export", -1))
    )
    store = make_store(tmp_path, DemandStore)
    store.save(definition)
    runtime = DemandRuntime(store)
    runtime.histories = {
        "sensor.house": channel("sensor.house", [1] * 240),
        "sensor.export": channel(
            "sensor.export", [2] * rejected_hours + [0.2] * (240 - rejected_hours)
        ),
    }
    now = START + timedelta(hours=240)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.issue == "demand_boundary_inconsistent"
    assert runtime.forecast.model == ("weekday-hour-v2" if rejected_hours == 24 else "startup-v2")
    assert runtime.forecast.quality == ("estimated" if rejected_hours == 24 else "provisional")
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=SolarConfiguration((), ()), forecast=None
    )
    source, configuration, report = forecast_basis(now, solar, demand=runtime)
    result = source.current(configuration, now, report=report)
    assert result.demand_evidence[0].forecast_version == runtime.forecast.version
    assert sum(interval.load_kwh for interval in result.problem.intervals) == pytest.approx(
        38.4 if rejected_hours == 24 else 20
    )


@pytest.mark.parametrize("fault", ["expired", "future", "wrong_configuration"])
def test_installation_rejects_unusable_demand_forecast_without_rewriting_it(tmp_path, fault):
    """A forecast must match the current demand definition and be known within the last 30 minutes."""
    from types import SimpleNamespace

    from open_energy_planner.home_assistant.observations import InputsUnavailable
    from open_energy_planner.solar.calculation.model import SolarConfiguration
    from tests.app.forecast.support.basis import forecast_basis

    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    assert runtime.install(runtime.build(runtime.job(START)), START)
    if fault == "wrong_configuration":
        runtime.forecast = replace(runtime.forecast, configuration_version="other-configuration")
    else:
        runtime.forecast = replace(
            runtime.forecast,
            issued_at=START - timedelta(minutes=30)
            if fault == "expired"
            else START + timedelta(seconds=1),
        )
    original = runtime.forecast
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=SolarConfiguration((), ()), forecast=None
    )
    source, configuration, report = forecast_basis(START, solar, demand=runtime)
    with pytest.raises(InputsUnavailable, match="demand_forecast_expired"):
        source.current(configuration, START, report=report)
    assert runtime.forecast == original


@pytest.mark.parametrize("gap", [False, True])
def test_demand_projection_crosses_quarter_without_requiring_past_forecast(tmp_path, gap):
    """One elapsed UTC-quarter second clips cached 0.4-kW demand and 2-kW PV. An interior gap blocks planning."""
    from types import SimpleNamespace

    from open_energy_planner.demand.calculation.model import ForecastStep
    from open_energy_planner.home_assistant.observations import InputsUnavailable
    from open_energy_planner.installation.location import SiteLocation
    from open_energy_planner.solar.calculation.model import (
        SolarConfiguration,
        SolarEnergy,
        SolarForecast,
        SolarInverter,
        SolarSource,
    )
    from tests.app.forecast.support.basis import forecast_basis

    now = START + timedelta(days=1, minutes=15, seconds=1)
    quarter = now.replace(second=0, microsecond=0)
    definition = config()
    store = make_store(tmp_path, DemandStore)
    store.save(definition)
    runtime = DemandRuntime(store)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    rows = tuple(
        ForecastStep(quarter + timedelta(minutes=15 * index_item), 0.25, 0.1)
        for index_item in range(3)
    )
    runtime.forecast = replace(runtime.forecast, steps=(rows[0], rows[2]) if gap else rows)
    solar_definition = SolarConfiguration(
        (SolarSource("pv", "sensor.pv", "pv_inv", 2000, "south"),),
        (SolarInverter("pv_inv", 4000, 1),),
    )
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None),
        configuration=solar_definition,
        forecast=SolarForecast(
            solar_definition.version,
            SiteLocation(52, 6, "Europe/Amsterdam"),
            now,
            "independent-two-kW-profile",
            tuple(SolarEnergy("pv", row.start, row.duration_h, 0.5, 0.5) for row in rows),
        ),
    )
    original = runtime.forecast, solar.forecast
    source, configuration, report = forecast_basis(now, solar, demand=runtime)
    if gap:
        with pytest.raises(InputsUnavailable, match="installation_forecast_coverage_missing"):
            source.current(configuration, now, report=report)
    else:
        result = source.current(configuration, now, report=report)
        assert result.problem.intervals[0].start == now
        assert result.problem.intervals[0].duration_h == pytest.approx(0.25 - 1 / 3600)
        for step in result.problem.intervals:
            assert step.load_kwh == pytest.approx(0.4 * step.duration_h)
            assert step.pv_kwh == pytest.approx(2 * step.duration_h)
        assert result.demand_evidence[0].forecast_version == runtime.forecast.version
    assert (runtime.forecast, solar.forecast) == original


def test_bounded_quarter_projection_preserves_independent_hour_energy():
    """Means 1, 2, 3 kWh/h give a limited unit slope: second-hour quarters sum to 2 kWh without flat replication."""
    profile = WeekdayProfile(3650, "UTC", tuple((3, hour, float(hour + 1)) for hour in range(24)))
    forecast = predict(config(), (), START, profile)
    second = forecast.steps[4:8]
    assert [row.kwh for row in second] == pytest.approx([0.40625, 0.46875, 0.53125, 0.59375])
    assert sum(row.kwh for row in second) == pytest.approx(2)
    assert all(row.kwh >= 0 for row in forecast.steps)


@pytest.mark.parametrize(
    "instant", [START, datetime(2026, 3, 29, tzinfo=UTC), datetime(2026, 10, 25, tzinfo=UTC)]
)
def test_estimated_quarters_recombine_into_the_same_hourly_means(instant):
    """Normal and DST UTC hours preserve every model mean and total. Flat and zero hours remain honest."""
    profile = WeekdayProfile(
        3650,
        "Europe/Amsterdam",
        tuple((day, hour, float(hour % 4)) for day in range(7) for hour in range(24)),
    )
    forecast = predict(config(), (), instant, profile)
    for offset in range(0, 96, 4):
        rows = forecast.steps[offset : offset + 4]
        assert sum(row.kwh for row in rows) == pytest.approx(profile.predict(rows[0].start))
        assert all(row.kwh >= 0 for row in rows)
    assert sum(row.kwh for row in forecast.steps) == pytest.approx(
        sum(profile.predict(instant + timedelta(hours=hour)) for hour in range(48))
    )


@pytest.mark.parametrize("duration", [0, -0.25, 0.1, 1.25, True, float("nan"), float("inf")])
def test_forecast_step_rejects_durations_outside_quarter_multiples(duration):
    """Final partial hours do not admit non-quarter, nonfinite, boolean, zero or oversized demand intervals."""
    from open_energy_planner.demand.calculation.model import ForecastStep

    with pytest.raises(ValueError, match="Unsupported demand resolution"):
        ForecastStep(START, duration, 0.1)
