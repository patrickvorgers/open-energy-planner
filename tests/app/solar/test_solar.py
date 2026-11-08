"""Independent physical, multiple-inverter, history and version-isolation solar scenarios."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.demand.calculation.model import ForecastStep
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.home_assistant.observations import InputsUnavailable
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarEnergy,
    SolarInverter,
    SolarSource,
    SolarWeatherHour,
    horizon,
    version,
)
from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
from open_energy_planner.solar.learning import fit
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import document
from tests.app.forecast.support.basis import forecast_basis
from tests.database.support.archive import (
    corrupt_document,
    list_documents,
    make_store,
    read_document,
)

START = datetime(2026, 6, 20, 0, tzinfo=UTC)
SITE = SiteLocation(52, 6, "Europe/Amsterdam")


def plant(*, count=1, shared=False, limit=5000, tilt=0, orientation="south"):
    """Declare disjoint 1 kWp sources with explicit standalone or shared inverter ratings."""
    return SolarConfiguration(
        tuple(
            SolarSource(
                f"pv{index_item}",
                f"sensor.pv{index_item}",
                "shared" if shared else f"i{index_item}",
                1000,
                orientation,
                tilt=tilt,
            )
            for index_item in range(count)
        ),
        (SolarInverter("shared", limit),)
        if shared
        else tuple(SolarInverter(f"i{index_item}", limit) for index_item in range(count)),
    )


def weather(now=START, *, days=3, ghi=600):
    """Provide complete independently fixed hourly radiation and temperature, never external weather."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(
        SolarWeatherHour(start + timedelta(hours=index_item), ghi, 500, 100, 20, 1, now)
        for index_item in range(days * 24)
    )


def test_horizontal_noon_power_matches_independent_pvwatts_temperature_arithmetic():
    """At 1000 W/m2 and 25 C ambient, 1 kWp yields 1*(1-.004*(1000/(25+6.84))) kW before 96% conversion."""
    noon = START + timedelta(hours=12)
    row = SolarWeatherHour(noon, 1000, 0, 1000, 25, 1, START)
    result = PVWattsPredictor().hourly(plant(), SITE, (row,))[0]
    expected = 1 * (1 - 0.004 * (1000 / (25 + 6.84)))
    assert result.dc_kwh == pytest.approx(expected)
    assert result.ac_kwh == pytest.approx(expected * 0.96)


def test_night_is_zero_even_with_inconsistent_positive_radiation():
    """Midnight sun geometry forces zero PV instead of producing from a malformed bright-night fixture."""
    result = PVWattsPredictor().hourly(plant(), SITE, (weather()[0],))[0]
    assert result.dc_kwh == result.ac_kwh == 0


@pytest.mark.parametrize("shared,limit,expected", [(True, 500, 0.5), (False, 500, 1.0)])
def test_multiple_sources_respect_shared_or_separate_inverter_limits(shared, limit, expected):
    """Two 1 kWp arrays share 0.5 kW once or each have 0.5 kW. Total AC must be 0.5 or 1.0 kWh."""
    rows = PVWattsPredictor().hourly(
        plant(count=2, shared=shared, limit=limit),
        SITE,
        (replace(weather()[12], ghi=1000, dni=0, dhi=1000),),
    )
    assert sum(row.ac_kwh for row in rows) == pytest.approx(expected)
    assert sum(row.dc_kwh for row in rows) > expected
    assert rows[0].source_id != rows[1].source_id


def test_east_west_is_equal_capacity_combination_with_distinct_daily_shapes():
    """East produces more at 07 UTC, west more at 17 UTC, and assumed east-west output equals their half-sum."""
    rows = (weather()[7], weather()[17])
    predictor = PVWattsPredictor()
    east = predictor.hourly(plant(tilt=30, orientation="east"), SITE, rows)
    west = predictor.hourly(plant(tilt=30, orientation="west"), SITE, rows)
    both = predictor.hourly(plant(tilt=30, orientation="east-west"), SITE, rows)
    assert east[0].dc_kwh > west[0].dc_kwh
    assert west[1].dc_kwh > east[1].dc_kwh
    for east_output, west_output, combined_output in zip(east, west, both, strict=True):
        assert combined_output.dc_kwh == pytest.approx(
            (east_output.dc_kwh + west_output.dc_kwh) / 2
        )


@pytest.mark.parametrize(
    "instant,groups", [(START, 120), (datetime(2026, 10, 25, 0, 15, tzinfo=UTC), 123)]
)
def test_forecast_preserves_48_hours_clock_hour_controls_and_per_source_energy(instant, groups):
    """Across normal and DST days, each source keeps 48 hours, clock-hour coarse controls and the hourly-energy integral."""
    forecast = PVWattsPredictor().forecast(plant(count=2), SITE, weather(instant), instant)
    assert len(forecast.energies) == 2 * groups
    for source in ("pv0", "pv1"):
        series = [
            source_item for source_item in forecast.energies if source_item.source_id == source
        ]
        assert sum(series_item.duration_h for series_item in series) == 48
        assert series[-1].start + timedelta(hours=series[-1].duration_h) == series[
            0
        ].start + timedelta(hours=48)
        quarters = [
            series_item
            for series_item in series
            if series_item.start.date() == instant.date() and series_item.start.hour == 12
        ]
        assert sum(source_item.ac_kwh for source_item in quarters) == pytest.approx(
            PVWattsPredictor().hourly(plant(), SITE, (weather(instant)[12],))[0].ac_kwh
        )


def test_missing_or_future_received_weather_cannot_complete_the_horizon():
    """Missing noon or weather learned after issue time causes explicit failure rather than zero filling."""
    for values in (
        weather()[:12] + weather()[13:],
        tuple(
            replace(weather_item, received_at=START + timedelta(seconds=1))
            for weather_item in weather()
        ),
    ):
        with pytest.raises(ValueError, match="coverage_missing"):
            PVWattsPredictor().forecast(plant(), SITE, values, START)


@pytest.mark.parametrize(
    "change",
    ["duplicate", "unknown_inverter", "negative_capacity", "bad_orientation", "boolean_capacity"],
)
def test_solar_configuration_rejects_ambiguous_or_invalid_source_mappings(change):
    """Duplicated meters/IDs, missing inverter links and invalid minimum inputs cannot become a forecast definition."""
    source = plant().sources[0]
    with pytest.raises((ValueError, TypeError)):
        if change == "duplicate":
            SolarConfiguration((source, source), plant().inverters)
        elif change == "unknown_inverter":
            SolarConfiguration((replace(source, inverter_id="missing"),), plant().inverters)
        else:
            replace(
                source,
                **{
                    "negative_capacity": {"capacity_w": -1},
                    "bad_orientation": {"orientation": "unknown"},
                    "boolean_capacity": {"capacity_w": True},
                }[change],
            )


def training_history(*, factor=0.8, days=35):
    """Supply 1 kWh daylight predictions and measured factor kWh/h with a reset-safe cumulative sum."""
    rows, predicted, total = [], [], 100.0
    start = START - timedelta(days=days, hours=1)
    for index_item in range(days * 24 + 1):
        stamp = start + timedelta(hours=index_item)
        total += factor if 8 <= stamp.hour < 16 else 0
        rows.append(EnergyHour(stamp, total, total % 20))
        predicted.append(SolarEnergy("pv0", stamp, 1, 1 / 0.96, 1))
    return EnergyHistory("sensor.pv0", tuple(rows)), tuple(predicted)


def test_local_regression_learns_per_source_factor_without_claiming_activation():
    """A measured 0.8 kWh per predicted 1 kWh daylight hour yields a regularized factor near 0.8 and remains shadow-only."""
    history, predicted = training_history()
    # Night measurements are excluded by the physical predictor's expected zero.
    predicted = tuple(
        replace(production_point, dc_kwh=0, ac_kwh=0)
        if not 8 <= production_point.start.hour < 16
        else production_point
        for production_point in predicted
    )
    model = fit(plant().sources[0], history, predicted, START, plant().inverters[0])
    assert model.factor == pytest.approx(0.8, abs=0.002)
    assert model.status == "waiting_for_accuracy_evidence"
    assert model.training_version
    assert (
        fit(
            replace(plant().sources[0], can_curtail=True),
            history,
            predicted,
            START,
            plant().inverters[0],
        )
        is None
    )
    assert fit(plant().sources[0], None, predicted, START, plant().inverters[0]) is None


def test_clipped_shared_inverter_hours_cannot_teach_available_dc_yield():
    """A source receiving only half of shared clipped AC output is not treated as a weak panel array."""
    history, predicted = training_history()
    clipped = tuple(replace(production_point, ac_kwh=0.5) for production_point in predicted)
    assert fit(plant().sources[0], history, clipped, START, plant().inverters[0]) is None


def test_hybrid_ac_history_cannot_identify_dc_yield_but_declared_dc_history_uses_dc_target():
    """Unknown hybrid battery routes block AC calibration. 0.8 measured DC against 1/0.96 predicted DC gives factor about 0.768."""
    history, predicted = training_history()
    predicted = tuple(
        replace(production_point, dc_kwh=0, ac_kwh=0)
        if not 8 <= production_point.start.hour < 16
        else production_point
        for production_point in predicted
    )
    inverter = replace(plant().inverters[0], hybrid=True)
    source = plant().sources[0]
    standalone = fit(source, history, predicted, START, replace(inverter, hybrid=False))
    assert standalone.factor == pytest.approx(0.8, abs=0.002)
    assert fit(source, history, predicted, START, inverter) is None
    result = fit(replace(source, measurement_boundary="dc"), history, predicted, START, inverter)
    assert result.factor == pytest.approx(0.8 * 0.96, abs=0.002)
    assert (
        fit(
            replace(source, measurement_boundary="dc", can_curtail=True),
            history,
            predicted,
            START,
            inverter,
        )
        is None
    )


@pytest.mark.parametrize(
    "hybrid,boundary,curtail,expected",
    [
        (False, "ac", False, "history_required"),
        (True, "ac", False, "hybrid_route_history_required"),
        (True, "dc", False, "history_required"),
        (True, "ac", True, "curtailment_feedback_required"),
        (True, "dc", True, "curtailment_feedback_required"),
    ],
)
def test_calibration_status_preserves_guard_precedence_and_history_failure(
    tmp_path, hybrid, boundary, curtail, expected
):
    """Missing route/control evidence takes precedence. Missing history retains its separate Recorder failure cause."""
    config = SolarConfiguration(
        (replace(plant().sources[0], measurement_boundary=boundary, can_curtail=curtail),),
        (replace(plant().inverters[0], hybrid=hybrid),),
    )
    store = make_store(tmp_path, SolarStore)
    store.save(config)
    runtime = SolarRuntime(store)
    runtime.history_issues["sensor.pv0"] = "unsupported_statistics"
    runtime.history_retry_after["sensor.pv0"] = START + timedelta(minutes=15)
    status = runtime.document(START)
    assert status["calibration_status"] == {"pv0": expected}
    assert status["history_issues"] == {"sensor.pv0": "unsupported_statistics"}


def test_runtime_archives_original_forecast_rejects_stale_workers_and_handles_corruption(
    tmp_path, monkeypatch
):
    """Archive a physical forecast once, reject a changed-definition result and fail explicitly on a corrupt current pointer."""
    store = make_store(tmp_path, SolarStore)
    monkeypatch.setattr(
        "open_energy_planner.solar.runtime.distribution_version",
        lambda name: "test-installed-version" if name == "pvlib" else None,
    )
    config = plant()
    store.save(config)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather())
    job = runtime.job(START)
    result = runtime.build(job)
    assert runtime.install(result, START)
    original = read_document(tmp_path, "solar", f"forecast-{runtime.forecast.version}.json")
    import json

    assert json.loads(original)["assumptions"]["pvlib"] == "test-installed-version"
    assert runtime.job(START) is None
    runtime.configure(plant(count=2))
    assert not runtime.install(result, START)
    runtime.configure(config)
    assert not runtime.install(result, START)
    assert read_document(tmp_path, "solar", f"forecast-{result[1].version}.json") == original
    corrupt_document(tmp_path, "solar", "current.json", '"../broken"')
    assert make_store(tmp_path, SolarStore).issue == "solar_configuration_unreadable"


def test_failed_history_source_does_not_block_other_inverters(tmp_path):
    """Record a Recorder failure for source one and advance to source two without inventing zero historical yield."""
    store = make_store(tmp_path, SolarStore)
    store.save(plant(count=2))
    runtime = SolarRuntime(store)
    first = runtime.request(START)
    runtime.receive(first["version"], None, START, error="unsupported_statistics")
    assert runtime.request(START)["entity_id"] == "sensor.pv1"
    assert runtime.histories == {}
    assert runtime.history_issues == {"sensor.pv0": "unsupported_statistics"}
    with pytest.raises(ValueError, match="changed"):
        runtime.receive(first["version"], None, START, error="history_unavailable")
    second = runtime.request(START)
    runtime.receive(second["version"], None, START, error="history_unavailable")
    assert runtime.request(START + timedelta(minutes=14)) is None
    assert runtime.request(START + timedelta(minutes=15))["entity_id"] == "sensor.pv0"


@pytest.mark.parametrize("hybrid", [False, True])
def test_basis_preserves_per_source_dc_and_converts_once_through_configured_inverters(
    tmp_path, hybrid
):
    """Two sources keep original DC energy. Each real inverter converts once at 96%, including declared hybrids."""
    now = START + timedelta(hours=12)
    store = make_store(tmp_path, SolarStore)
    definition = plant(count=2)
    definition = replace(
        definition,
        inverters=tuple(replace(inverter, hybrid=hybrid) for inverter in definition.inverters),
    )
    store.save(definition)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather(now))
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.forecast
    source, configuration, report = forecast_basis(now, runtime)
    basis = source.current(configuration, now, report=report)
    assert basis.problem.grid_import_limit_w == basis.problem.grid_export_limit_w == 4000
    expected_dc = {row.source_id: row.dc_kwh for row in original.energies if row.start == now}
    expected_ac = sum(row.ac_kwh for row in original.energies if row.start == now)
    assert dict(basis.problem.intervals[0].pv_sources) == pytest.approx(expected_dc)
    assert basis.problem.intervals[0].pv_kwh == pytest.approx(sum(expected_dc.values()))
    assert basis.problem.inverters == definition.inverters
    assert basis.solar_evidence[0].source_ids == ("pv0", "pv1")
    result = HighsSolver().solve(basis.problem)
    assert result.plan is not None, result
    validate(basis.problem, result.plan)
    assert sum(inverter.export_kwh for inverter in result.plan.flows[0].inverters) == pytest.approx(
        expected_ac
    )
    assert runtime.forecast == original


def test_basis_ignores_elapsed_cached_intervals_and_integrates_partial_current_interval():
    """Seven elapsed minutes leave 8/15 of the current quarter's declared household and per-source DC energy."""
    quarter = START + timedelta(hours=12, minutes=15)
    now = quarter + timedelta(minutes=7)
    definition = plant()
    forecast = PVWattsPredictor().forecast(definition, SITE, weather(quarter), quarter)
    runtime = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=definition, forecast=forecast
    )
    source, configuration, report = forecast_basis(
        now, runtime, load_steps=(ForecastStep(quarter, 0.25, 0.25),)
    )
    result = source.current(configuration, now, report=report)
    assert len(result.problem.intervals) == 1
    first = result.problem.intervals[0]
    assert first.start == now
    assert first.duration_h == pytest.approx(8 / 60)
    assert first.load_kwh == pytest.approx(0.25 * 8 / 15)
    expected = next(row.dc_kwh for row in forecast.energies if row.start == quarter)
    assert first.pv_kwh == pytest.approx(expected * 8 / 15)
    assert runtime.forecast == forecast


def test_basis_trims_only_future_tail_and_still_rejects_internal_solar_gaps():
    """An extra cached household hour cannot extend 48-hour planning. A missing noon PV quarter remains a fault."""
    definition = plant(count=2)
    forecast = PVWattsPredictor().forecast(definition, SITE, weather(), START)
    runtime = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=definition, forecast=forecast
    )
    steps = tuple(ForecastStep(start, duration, duration) for start, duration in horizon(START))
    steps += (ForecastStep(START + timedelta(hours=48), 1, 1),)
    source, configuration, report = forecast_basis(START, runtime, load_steps=steps)
    original_load = source.demand.forecast
    result = source.current(configuration, START, report=report)
    assert len(result.problem.intervals) == 120
    last = result.problem.intervals[-1]
    assert last.start + timedelta(hours=last.duration_h) == START + timedelta(hours=48)
    assert last.load_kwh == last.duration_h == 1
    assert sum(interval.pv_kwh for interval in result.problem.intervals) == pytest.approx(
        sum(row.dc_kwh for row in forecast.energies)
    )
    assert source.demand.forecast == original_load
    runtime.forecast = replace(
        forecast,
        energies=tuple(
            row
            for row in forecast.energies
            if not (row.source_id == "pv0" and row.start == START + timedelta(hours=12))
        ),
    )
    with pytest.raises(InputsUnavailable, match="coverage_missing"):
        source.current(configuration, START, report=report)


def test_sixteen_source_basis_indexes_forecast_rows_once_and_preserves_total_energy():
    """Index 1920 source records once for 120 planning steps. Per-source DC totals retain the independent forecasts."""
    definition = plant(count=16)
    forecast = PVWattsPredictor().forecast(definition, SITE, weather(), START)
    scans = []

    class Rows:
        """Count full-source scans independently of overlap arithmetic or timing-sensitive assertions."""

        def __iter__(self):
            """Expose the unchanged immutable source records and count each enumeration."""
            scans.append(1)
            return iter(forecast.energies)

    runtime = SimpleNamespace(
        store=SimpleNamespace(issue=None),
        configuration=definition,
        forecast=SimpleNamespace(
            configuration_version=definition.version,
            issued_at=START,
            valid_until=None,
            location=forecast.location,
            energies=Rows(),
            version=forecast.version,
            weather_version=forecast.weather_version,
            model=forecast.model,
            quality=forecast.quality,
        ),
    )
    source, configuration, report = forecast_basis(START, runtime)
    result = source.current(configuration, START, report=report)
    assert scans == [1]
    assert len(result.problem.intervals) == 120
    assert sum(step.pv_kwh for step in result.problem.intervals) == pytest.approx(
        sum(row.dc_kwh for row in forecast.energies)
    )
    for pv_source in definition.sources:
        assert sum(
            dict(step.pv_sources)[pv_source.source_id] for step in result.problem.intervals
        ) == pytest.approx(
            sum(row.dc_kwh for row in forecast.energies if row.source_id == pv_source.source_id)
        )


def test_empty_solar_configuration_ignores_stale_forecast_without_weather_or_history(tmp_path):
    """No declared panels means exact zero production even if an old solar forecast remains cached."""
    store = make_store(tmp_path, SolarStore)
    store.save(SolarConfiguration((), ()))
    runtime = SolarRuntime(store)
    original = PVWattsPredictor().forecast(plant(), SITE, weather(), START)
    runtime.forecast = original
    source, configuration, report = forecast_basis(START, runtime)
    result = source.current(configuration, START, report=report)
    assert all(
        interval.pv_kwh == 0 and interval.pv_sources == () for interval in result.problem.intervals
    )
    assert result.problem.pv_sources == result.problem.inverters == ()
    assert result.solar_evidence == ()
    assert runtime.forecast == original
    assert runtime.job(START) is runtime.request(START) is None


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "future",
        "wrong_configuration",
        "unreadable_configuration",
        "missing_forecast",
        "wrong_location",
    ],
)
def test_installation_blocks_unusable_solar_cache_without_replacing_its_evidence(fault):
    """Expired, future, mismatched or unreadable solar evidence blocks planning without modifying the cached original."""
    definition = plant()
    forecast = PVWattsPredictor().forecast(definition, SITE, weather(), START)
    runtime = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=definition, forecast=forecast
    )
    if fault == "expired":
        runtime.forecast = replace(forecast, issued_at=START - timedelta(minutes=30))
    elif fault == "future":
        runtime.forecast = replace(forecast, issued_at=START + timedelta(seconds=1))
    elif fault == "wrong_configuration":
        runtime.forecast = replace(forecast, configuration_version="other-configuration")
    elif fault == "unreadable_configuration":
        runtime.store.issue = "solar_configuration_unreadable"
    elif fault == "missing_forecast":
        runtime.forecast = None
    else:
        runtime.forecast = replace(forecast, location=SiteLocation(53, 6, "Europe/Amsterdam"))
    original = runtime.forecast
    source, configuration, report = forecast_basis(START, runtime)
    with pytest.raises(
        InputsUnavailable,
        match="solar_configuration_required"
        if runtime.store.issue
        else "solar_forecast_unavailable",
    ):
        source.current(configuration, START, report=report)
    assert runtime.forecast == original


def test_runtime_uses_existing_history_for_an_independent_candidate_only_on_the_measured_source(
    tmp_path,
):
    """A measured roof trains a direct model while its neighbor and selected baseline remain independent."""
    from tests.app.solar.support import frozen_runs

    config = plant(count=2)
    values = tuple(
        replace(row, received_at=START) for row in weather(START - timedelta(days=56), days=59)
    )
    physical = PVWattsPredictor().hourly(config, SITE, values)
    rows, total = [], 100.0
    for row in physical:
        if row.source_id == "pv0" and row.start < START:
            total += row.ac_kwh * (0.25 if row.start.hour < 11 else 0.9)
            rows.append(EnergyHour(row.start, total, total))
    store = make_store(tmp_path, SolarStore)
    store.save(config)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, values)
    runtime.set_forecast_runs(SITE, frozen_runs(values, SITE, START))
    runtime.histories["sensor.pv0"] = EnergyHistory("sensor.pv0", tuple(rows))
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert runtime.forecast.model == "pvwatts-v2"
    assert runtime.candidate.model == "solar-xgboost-production-v2"
    assert {row.source_id for row in runtime.candidate.energies} == {"pv0"}
    assert store.documents.read("quality-source-models-" + runtime.candidate.version + ".json") == {
        "pv0": "solar-xgboost-production-v2",
    }
    assert store.documents.read("quality-source-models-" + runtime.forecast.version + ".json") == {
        "pv0": "pvwatts-v2",
        "pv1": "pvwatts-v2",
    }
    assert (
        runtime.learning["pv0"].summary.tree_mae_kwh
        < runtime.learning["pv0"].summary.baseline_mae_kwh
    )
    assert [name for name in list_documents(tmp_path, "solar") if name.startswith("ml-training-")]
    assert runtime.histories["sensor.pv0"].hours == tuple(rows)


def test_daily_evidence_blocks_reuse_values_and_reconstruct_receipt_times(tmp_path):
    """Refetch identical weather with a later receipt: daily values are reused while each version reconstructs its own knowledge time."""
    import json

    store = make_store(tmp_path, SolarStore)
    original = [document(weather_item) for weather_item in weather(days=2)]
    revised = [
        {**row, "received_at": (START + timedelta(hours=1)).isoformat(sep=" ")} for row in original
    ]
    store.archive_series("weather", version(original), original)
    count = len([name for name in list_documents(tmp_path, "solar") if name.startswith("block-")])
    store.archive_series("weather", version(revised), revised)
    assert (
        len([name for name in list_documents(tmp_path, "solar") if name.startswith("block-")])
        == count
        == 2
    )
    for rows in (original, revised):
        manifest = json.loads(read_document(tmp_path, "solar", f"weather-{version(rows)}.json"))
        restored = []
        for item in manifest["blocks"]:
            values = json.loads(read_document(tmp_path, "solar", f"block-{item['block']}.json"))[
                "hours"
            ]
            receipts = dict(item["receipts"])
            received = None
            for index, row in enumerate(values):
                received = receipts.get(index, received)
                restored.append({**row, "received_at": received})
        assert restored == rows


def test_prediction_projects_only_horizon_weather_even_when_annual_history_exists(monkeypatch):
    """Annual training weather must not enter the 48-hour projection loop. Output stays identical to the same future-only inputs."""
    values = tuple(
        replace(weather_item, received_at=START)
        for weather_item in weather(START - timedelta(days=365), days=368)
    )
    predictor = PVWattsPredictor()
    quarters = predictor.quarters
    sizes = []

    def bounded(configuration, location, rows, **kwargs):
        """Observe the production model boundary without changing any physical output."""
        sizes.append(len(rows))
        return quarters(configuration, location, rows, **kwargs)

    monkeypatch.setattr(predictor, "quarters", bounded)
    with_history = predictor.forecast(plant(), SITE, values, START)
    future_only = predictor.forecast(plant(), SITE, weather(), START)
    assert with_history.energies == future_only.energies
    assert sizes == [48, 48]


def test_quarter_solar_uses_changing_geometry_and_conserves_hourly_calibration_energy():
    """Four east-facing morning quarters differ under constant hourly weather. Their sum equals calibration energy."""
    source = plant(tilt=30, orientation="east")
    hour = weather()[7]
    predictor = PVWattsPredictor()
    rows = predictor.quarters(source, SITE, (hour,))
    assert [row.start.minute for row in rows] == [0, 15, 30, 45]
    assert len({round(row.dc_kwh, 8) for row in rows}) == 4
    assert all(row.duration_h == 0.25 for row in rows)
    assert sum(row.dc_kwh for row in rows) == pytest.approx(
        predictor.hourly(source, SITE, (hour,))[0].dc_kwh
    )
    forecast = predictor.forecast(source, SITE, weather(), START)
    projected = [
        row for row in forecast.energies if row.start.hour == 7 and row.start.date() == START.date()
    ]
    assert [row.dc_kwh for row in projected] == pytest.approx([row.dc_kwh for row in rows])


def test_shared_inverter_is_capped_in_every_quarter():
    """Two arrays share 0.5 kW per quarter: total AC is at most 0.125 kWh, never one cap per array."""
    source = plant(count=2, shared=True, limit=500)
    rows = PVWattsPredictor().quarters(
        source, SITE, (replace(weather()[12], ghi=1000, dni=0, dhi=1000),)
    )
    for minute in (0, 15, 30, 45):
        assert sum(row.ac_kwh for row in rows if row.start.minute == minute) == pytest.approx(0.125)


def test_diffuse_hourly_weather_has_energy_conserving_quarter_timing():
    """Diffuse means 100/200/300 W/m2 give middle-quarter irradiance 162.5/187.5/212.5/237.5, not a flat hour."""
    rows = tuple(
        replace(weather()[11 + index], ghi=value, dni=0, dhi=value, celsius=25)
        for index, value in enumerate((100, 200, 300))
    )
    predicted = PVWattsPredictor().quarters(plant(), SITE, rows)
    middle = [row for row in predicted if row.start.hour == 12]
    expected = [
        value / 1000 * (1 - 0.004 * value / (25 + 6.84)) * 0.25
        for value in (162.5, 187.5, 212.5, 237.5)
    ]
    assert [row.dc_kwh for row in middle] == pytest.approx(expected)
    assert sum((162.5, 187.5, 212.5, 237.5)) / 4 == 200


def test_annual_calibration_constructs_only_hourly_energy_rows(monkeypatch):
    """A full year retains all 8760 calibration hours without allocating 35040 temporary quarter energy objects."""
    from open_energy_planner.solar.calculation import predictor as adapter

    weather_hours = weather(days=365)
    energy_row_count = 0
    energy_row_constructor = adapter.SolarEnergy

    def count_rows(*args, **kwargs):
        """Count actual immutable energy allocations without changing their validation or quantities."""
        nonlocal energy_row_count
        energy_row_count += 1
        return energy_row_constructor(*args, **kwargs)

    monkeypatch.setattr(adapter, "SolarEnergy", count_rows)
    hourly_energy_rows = PVWattsPredictor().hourly(plant(), SITE, weather_hours)
    assert len(hourly_energy_rows) == energy_row_count == 8760
    assert hourly_energy_rows[0].start == START
    assert hourly_energy_rows[-1].start == START + timedelta(hours=8759)
    assert all(hourly_energy.duration_h == 1 for hourly_energy in hourly_energy_rows)
    quarter_energy_rows = PVWattsPredictor().quarters(plant(), SITE, weather_hours[:24])
    for hourly_energy in hourly_energy_rows[:24]:
        assert hourly_energy.dc_kwh == pytest.approx(
            sum(
                quarter_energy.dc_kwh
                for quarter_energy in quarter_energy_rows
                if quarter_energy.start.hour == hourly_energy.start.hour
            )
        )
