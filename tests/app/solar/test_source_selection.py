"""Select solar sources only from independently specified forward forecasts and measured sums."""

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.solar.calculation.model import (
    SolarEnergy,
    SolarForecast,
    SolarImport,
    SolarPanel,
)
from open_energy_planner.solar.ha_forecasts import (
    HAProductionForecast,
    ProductionInterval,
    parse_ha_predictions,
)
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.source_selection import choose_sources, comparison_hours, ha_energies
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import document
from tests.app.forecast.support.basis import forecast_basis
from tests.app.solar.support import SITE, START, plant, weather
from tests.database.support.archive import make_store


def prediction(start, amount, *, configuration=None, model="pvwatts-v2"):
    """Give two complete future days a fixed hourly energy independent of model calculations."""
    configuration = configuration or plant()
    return SolarForecast(
        configuration.version,
        SITE,
        start,
        "weather",
        tuple(
            SolarEnergy("pv0", start + timedelta(hours=index), 1, amount / 0.96, amount)
            for index in range(48)
        ),
        model=model,
    )


def provider(start=START, *, refreshed=True, missing=None):
    """Declare original HA hourly AC production and an independently observed refresh."""
    metadata = SolarImport("forecast_solar", "entry", (SolarPanel("plane", 1000, 180, 30),))
    return HAProductionForecast(
        metadata,
        tuple(
            ProductionInterval(start + timedelta(hours=index), 1, 0.4)
            for index in range(48)
            if index != missing
        ),
        start,
        start if refreshed else None,
        "configuration-a",
    )


def test_accuracy_windows_follow_ha_calendar_and_only_whole_recorder_hours():
    """HA spring/autumn days retain 23/25 hours. Fractional offset edges never fabricate half-hour actuals."""
    assert len(comparison_hours(date(2026, 3, 29), "Europe/Amsterdam")[0]) == 23
    assert len(comparison_hours(date(2026, 10, 25), "Europe/Amsterdam")[0]) == 25
    hours, _end = comparison_hours(date(2026, 6, 21), "Asia/Kolkata")
    assert len(hours) == 23
    assert all(instant.minute == 0 for instant in hours)


def test_ha_conversion_applies_declared_efficiency_once_and_preserves_missing_coverage():
    """400 Wh AC becomes 416.667 Wh standalone-equivalent DC, with no zero fill across a missing hour."""
    baseline = prediction(START, 0.5)
    rows, issues = ha_energies(plant(), baseline, {"sensor.pv0": (provider(),)}, START)
    assert not issues
    assert rows["pv0"][0].ac_kwh == 0.4
    assert rows["pv0"][0].dc_kwh * 0.96 == pytest.approx(0.4)
    rows, issues = ha_energies(plant(), baseline, {"sensor.pv0": (provider(missing=5),)}, START)
    assert rows == {}
    assert "incomplete" in issues["pv0"]


def test_native_quarters_integrate_without_losing_cloud_timing():
    """Four original 0.1/0.2/0.3/0.4 kWh quarters yield exactly 1 kWh for the completed first hour."""
    value = provider()
    quarters = tuple(
        ProductionInterval(START + timedelta(minutes=index * 15), 0.25, energy)
        for index, energy in enumerate((0.1, 0.2, 0.3, 0.4))
    )
    value = replace(value, steps=quarters + value.steps[1:])
    rows, issues = ha_energies(plant(), prediction(START, 0.5), {"sensor.pv0": (value,)}, START)
    assert not issues
    assert rows["pv0"][0].ac_kwh == pytest.approx(1)
    assert rows["pv0"][0].dc_kwh * 0.96 == pytest.approx(1)


@pytest.mark.parametrize("age", [None, timedelta(hours=2)])
def test_cache_discovery_and_expired_refresh_do_not_establish_freshness(age):
    """An unknown or exactly expired provider refresh cannot activate an HA planning forecast."""
    value = provider(refreshed=age is not None)
    now = START + age if age is not None else START
    rows, issues = ha_energies(plant(), prediction(START, 0.5), {"sensor.pv0": (value,)}, now)
    assert rows == {}
    assert "refresh" in issues["pv0"]


def test_ha_pure_pv_forecast_is_comparable_on_a_hybrid():
    """A pure PV meter admits 0.4 AC kWh and converts through efficiency exactly once."""
    configuration = plant()
    configuration = replace(
        configuration, inverters=(replace(configuration.inverters[0], hybrid=True),)
    )
    rows, issues = ha_energies(
        configuration, prediction(START, 0.5), {"sensor.pv0": (provider(),)}, START
    )
    assert issues == {}
    assert rows["pv0"][0].ac_kwh == pytest.approx(0.4)
    assert rows["pv0"][0].dc_kwh == pytest.approx(0.4 / 0.96)


@pytest.mark.parametrize(
    "second_model,with_weather", [("ha", False), ("ha", True), ("pvwatts", True)]
)
def test_shared_hybrid_pv_models_retain_one_common_ac_limit(tmp_path, with_weather, second_model):
    """Two pure PV meters share 500 W. HA and mixed forecasts cannot each spend the full inverter rating."""
    configuration = plant(count=2, shared=True, limit=500)
    configuration = replace(
        configuration,
        sources=(
            replace(configuration.sources[0], forecast_source="ha"),
            replace(configuration.sources[1], forecast_source=second_model),
        ),
        inverters=(replace(configuration.inverters[0], hybrid=True),),
    )
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather(ghi=1000) if with_weather else ())
    runtime.receive_ha(
        {
            "sensor.pv0": (provider(),),
            "sensor.pv1": (
                replace(
                    provider(), configuration=replace(provider().configuration, entry_id="other")
                ),
            ),
        }
    )
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert runtime.active_model("pv0") == "ha-production-v1"
    assert runtime.active_model("pv1") == (
        "ha-production-v1" if second_model == "ha" else "pvwatts-v2"
    )
    energies = runtime.forecast.energies
    intervals = {(row.start, row.duration_h) for row in energies}
    totals = {
        (start, duration): sum(
            row.ac_kwh for row in energies if row.start == start and row.duration_h == duration
        )
        for start, duration in intervals
    }
    assert max(total / duration for (_, duration), total in totals.items()) == pytest.approx(0.5)
    assert all(total <= 0.5 * duration + 1e-12 for (_, duration), total in totals.items())
    assert max(
        total for (_, duration), total in totals.items() if duration == 0.25
    ) == pytest.approx(0.125)
    hours = {start.replace(minute=0) for start, _duration in totals}
    assert max(
        sum(
            total for (start, _duration), total in totals.items() if start.replace(minute=0) == hour
        )
        for hour in hours
    ) == pytest.approx(0.5)
    assert next(row for row in energies if row.source_id == "pv0").dc_kwh == pytest.approx(
        0.1 / 0.96
    )


@pytest.mark.parametrize("physical_kwh,expected_kwh", [(0.1, 0.9), (0.4, 1.0)])
def test_mixed_selection_caps_only_the_final_combination(
    tmp_path, monkeypatch, physical_kwh, expected_kwh
):
    """HA A=.8 plus PVWatts B=.1 must retain .9 kWh. B=.4 exceeds 1 kW and is capped once."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor

    configuration = plant(count=2, shared=True, limit=1000)
    configuration = replace(
        configuration,
        sources=(
            replace(configuration.sources[0], forecast_source="ha"),
            replace(configuration.sources[1], forecast_source="pvwatts"),
        ),
    )
    baseline = prediction(START, physical_kwh, configuration=configuration)
    baseline = replace(
        baseline,
        energies=tuple(
            replace(row, source_id=source.source_id)
            for source in configuration.sources
            for row in baseline.energies
        ),
    )
    monkeypatch.setattr(PVWattsPredictor, "forecast", lambda *_arguments, **_options: baseline)
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather())
    value = provider()
    value = replace(value, steps=tuple(replace(step, kwh=0.8) for step in value.steps))
    runtime.receive_ha(
        {
            "sensor.pv0": (value,),
            "sensor.pv1": (
                replace(value, configuration=replace(value.configuration, entry_id="other")),
            ),
        }
    )
    assert runtime.install(runtime.build(runtime.job(START)), START)
    first = [row for row in runtime.forecast.energies if row.start == START]
    assert sum(row.ac_kwh for row in first) == pytest.approx(expected_kwh)
    assert next(row for row in first if row.source_id == "pv0").dc_kwh == pytest.approx(0.8 / 0.96)
    assert runtime.active_model("pv0") == "ha-production-v1"
    assert runtime.active_model("pv1") == "pvwatts-v2"


def test_ha_comparison_is_independent_of_sibling_provider_revision(tmp_path):
    """Changing only sibling HA from .8 to .1 kWh and its revision leaves A's own .8-kWh captured candidate unchanged."""
    configuration = plant(count=2, shared=True, limit=1000)
    baseline = prediction(START, 0.1, configuration=configuration)
    baseline = replace(
        baseline,
        energies=tuple(
            replace(row, source_id=source.source_id)
            for source in configuration.sources
            for row in baseline.energies
        ),
    )
    captures = []
    for sibling_kwh, sibling_revision in [(0.8, "sibling-a"), (0.1, "sibling-b")]:
        store = make_store(tmp_path / sibling_revision, SolarStore)
        value = provider()
        own = replace(value, steps=tuple(replace(step, kwh=0.8) for step in value.steps))
        sibling = replace(
            value,
            steps=tuple(replace(step, kwh=sibling_kwh) for step in value.steps),
            configuration_revision=sibling_revision,
        )
        energies, issues = ha_energies(
            configuration, baseline, {"sensor.pv0": (own,), "sensor.pv1": (sibling,)}, START
        )
        assert not issues
        choose_sources(
            store,
            configuration,
            baseline,
            None,
            (),
            energies,
            {},
            START,
            ha_configuration_versions={"sensor.pv0": ["own-a"], "sensor.pv1": [sibling_revision]},
        )
        captures.append(
            next(
                record
                for record in store.documents.iter_prefix("source-day-")
                if record["source_id"] == "pv0"
            )
        )
    for capture in captures:
        assert capture["models"]["ha"] == pytest.approx([0.8] * 24)
        assert capture["ha_configuration_versions"] == ["own-a"]
    assert captures[0] == captures[1]


@pytest.mark.parametrize("hybrid", [False, True])
def test_automatic_uses_three_identical_complete_days_and_survives_restart(tmp_path, hybrid):
    """HA's zero error beats PVWatts' 0.1 kWh and ML's 0.2 kWh on the same 72 realized hours."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    configuration = replace(
        configuration, inverters=(replace(configuration.inverters[0], hybrid=hybrid),)
    )
    for day in range(3):
        now = START + timedelta(days=day)
        baseline = prediction(now, 0.5)
        challenger = prediction(now, 0.6, model="pvwatts-calibrated-v2")
        ha = {"pv0": prediction(now, 0.4).energies}
        chosen, status = choose_sources(
            store,
            configuration,
            baseline,
            challenger,
            (SimpleNamespace(source_id="pv0"),),
            ha,
            {},
            now,
            ha_configuration_versions={"sensor.pv0": ["revision-a"]},
        )
        assert chosen["pv0"][0] == "pvwatts"
        assert status["pv0"]["mae_kwh"] == {}
    restored = SolarStore(store.documents)
    now = START + timedelta(days=4, hours=1)
    history = EnergyHistory(
        "sensor.pv0",
        tuple(
            EnergyHour(START + timedelta(hours=index), index * 0.4, index * 0.4)
            for index in range(4 * 24 + 1)
        ),
    )
    chosen, status = choose_sources(
        restored,
        configuration,
        prediction(now, 0.5),
        prediction(now, 0.6),
        (SimpleNamespace(source_id="pv0"),),
        {"pv0": prediction(now, 0.4).energies},
        {"sensor.pv0": history},
        now,
        ha_configuration_versions={"sensor.pv0": ["revision-a"]},
    )
    assert chosen["pv0"][0] == "ha"
    assert status["pv0"]["complete_days"] == 3
    assert status["pv0"]["comparison_days"] == [
        (START.date() + timedelta(days=offset)).isoformat() for offset in (3, 2, 1)
    ]
    assert status["pv0"]["mae_kwh"] == pytest.approx(
        {"pvwatts": 0.1, "ml": 0.2, "ha": 0}, abs=1e-12
    )
    changed, changed_status = choose_sources(
        restored,
        configuration,
        prediction(now, 0.5),
        prediction(now, 0.6),
        (SimpleNamespace(source_id="pv0"),),
        {"pv0": prediction(now, 0.4).energies},
        {"sensor.pv0": history},
        now,
        ha_configuration_versions={"sensor.pv0": ["revision-b"]},
    )
    assert changed["pv0"][0] == "pvwatts"
    assert changed_status["pv0"]["complete_days"] == 0
    # A single missing sum prevents a complete matched day. No candidate gets fewer hours.
    broken = replace(
        history, hours=tuple(hour for index, hour in enumerate(history.hours) if index != 40)
    )
    chosen, status = choose_sources(
        restored,
        configuration,
        prediction(now, 0.5),
        prediction(now, 0.6),
        (SimpleNamespace(source_id="pv0"),),
        {"pv0": prediction(now, 0.4).energies},
        {"sensor.pv0": broken},
        now,
        ha_configuration_versions={"sensor.pv0": ["revision-a"]},
    )
    assert chosen["pv0"][0] == "pvwatts"
    assert status["pv0"]["mae_kwh"] == {}


@pytest.mark.parametrize(
    "count,shared,boundary,actual_kwh,sibling_present,curtailed,expected_days",
    [
        (2, True, "ac", 2.5, True, False, 0),
        (2, True, "ac", 2.4, True, False, 3),
        (1, False, "ac", 2.5, False, False, 3),
        (2, False, "ac", 2.5, True, False, 3),
        (2, True, "dc", 2.5, True, False, 3),
        (2, True, "ac", 2.4, False, False, 0),
        (2, True, "ac", 2.4, True, True, 0),
        (1, False, "ac", 5.0, False, False, 0),
    ],
)
def test_automatic_requires_unclipped_comparable_meter_days(
    tmp_path, count, shared, boundary, actual_kwh, sibling_present, curtailed, expected_days
):
    """Two 2.5-kWh AC meters fill 5 kW and cannot promote ML. Below-limit/DC/independent days remain usable, missing data never means zero."""
    from open_energy_planner.solar.curtailment import CurtailmentHistory

    configuration = plant(count=count, shared=shared, limit=5000)
    configuration = replace(
        configuration,
        sources=tuple(
            replace(
                source,
                capacity_w=5000,
                measurement_boundary=boundary,
                curtailment_entity_id="binary_sensor.limited"
                if curtailed and source.source_id == "pv0"
                else None,
            )
            for source in configuration.sources
        ),
    )
    store = make_store(tmp_path, SolarStore)
    fitted = tuple(SimpleNamespace(source_id=source.source_id) for source in configuration.sources)

    def forecast(now, amount):
        """Supply independently specified hourly boundary values for each disjoint meter."""
        original = prediction(now, amount, configuration=configuration)
        return replace(
            original,
            energies=tuple(
                replace(row, source_id=source.source_id, ac_kwh=amount, dc_kwh=amount)
                for source in configuration.sources
                for row in original.energies
            ),
        )

    for day in range(3):
        now = START + timedelta(days=day)
        choose_sources(
            store,
            configuration,
            forecast(now, actual_kwh + 0.1),
            forecast(now, actual_kwh),
            fitted,
            {},
            {},
            now,
        )
    original_captures = list(store.documents.iter_prefix("source-day-"))
    now = START + timedelta(days=4, hours=1)
    histories = {
        source.entity_id: EnergyHistory(
            source.entity_id,
            tuple(
                EnergyHour(START + timedelta(hours=index), index * actual_kwh, index * actual_kwh)
                for index in range(97)
            ),
        )
        for source in configuration.sources
        if source.source_id == "pv0" or sibling_present
    }
    feedback = (
        {
            "binary_sensor.limited": CurtailmentHistory(
                "binary_sensor.limited", now, ((START - timedelta(days=1), True),)
            )
        }
        if curtailed
        else {}
    )
    chosen, statuses = choose_sources(
        store,
        configuration,
        forecast(now, actual_kwh + 0.1),
        forecast(now, actual_kwh),
        fitted,
        {},
        histories,
        now,
        feedback=feedback,
    )
    assert statuses["pv0"]["complete_days"] == expected_days
    assert chosen["pv0"][0] == ("ml" if expected_days else "pvwatts")
    assert statuses["pv0"]["mae_kwh"] == (
        pytest.approx({"pvwatts": 0.1, "ml": 0}) if expected_days else {}
    )
    assert all(
        capture in list(store.documents.iter_prefix("source-day-")) for capture in original_captures
    )


def test_capture_waits_for_every_available_candidate_then_freezes_the_same_build(tmp_path):
    """A short cached HA horizon cannot freeze a PVWatts-only tomorrow before the next real refresh."""
    store = make_store(tmp_path, SolarStore)
    choose_sources(
        store,
        plant(),
        prediction(START, 0.5),
        None,
        (),
        {"pv0": prediction(START, 0.4).energies[:24]},
        {},
        START,
    )
    assert list(store.documents.iter_prefix("source-day-")) == []
    later = START + timedelta(minutes=30)
    choose_sources(
        store,
        plant(),
        prediction(later, 0.7),
        None,
        (),
        {"pv0": prediction(later, 0.45).energies},
        {},
        later,
    )
    captured = list(store.documents.iter_prefix("source-day-"))
    assert len(captured) == 1
    assert captured[0]["captured_at"] == later.isoformat()
    assert captured[0]["models"]["pvwatts"] == pytest.approx([0.7] * 24)
    assert captured[0]["models"]["ha"] == pytest.approx([0.45] * 24)
    choose_sources(
        store,
        plant(),
        prediction(later, 0.9),
        None,
        (),
        {"pv0": prediction(later, 0.95).energies},
        {},
        later,
    )
    assert list(store.documents.iter_prefix("source-day-")) == captured


@pytest.mark.parametrize("failure", ["shape", "json", "io"])
@pytest.mark.parametrize(
    ("requested", "expected"), [("automatic", "pvwatts-v2"), ("ha", "ha-production-v1")]
)
def test_unusable_optional_accuracy_evidence_keeps_the_physical_forecast(
    tmp_path, monkeypatch, failure, requested, expected
):
    """Bad optional comparisons cannot block the valid baseline or a manually selected fresh HA forecast."""
    from open_energy_planner.solar.basis import forecast_usable
    from open_energy_planner.solar.source_selection import family

    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    store.save(
        replace(
            configuration, sources=(replace(configuration.sources[0], forecast_source=requested),)
        )
    )
    path = "source-day-" + family(plant(), plant().sources[0], SITE) + "-2026-06-19.json"
    bad = {"broken": True}
    store.documents.write(path, bad)
    read = store.documents.read

    def damaged(name):
        """Emulate malformed JSON or failed database reads only for the retained comparison."""
        if name == path and failure == "json":
            raise ValueError("Invalid JSON")
        if name == path and failure == "io":
            raise OSError("Comparison read unavailable")
        return read(name)

    monkeypatch.setattr(store.documents, "read", damaged)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather())
    predictions = {"sensor.pv0": (provider(),)}
    runtime.save_ha(predictions)
    runtime.receive_ha(predictions)
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert runtime.active_model("pv0") == expected
    assert runtime.issue is None
    assert "accuracy evidence" in runtime.source_selection["pv0"]["issue"]
    assert forecast_usable(runtime.configuration, runtime.forecast, SITE, START)
    assert read(path) == bad


def test_manual_source_is_explicit_and_unavailable_ml_falls_back(tmp_path):
    """Manual HA needs no accuracy warm-up, while unavailable ML explicitly retains PVWatts."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    for requested, expected in (("ha", "ha"), ("ml", "pvwatts")):
        settings = replace(
            configuration, sources=(replace(configuration.sources[0], forecast_source=requested),)
        )
        chosen, statuses = choose_sources(
            store,
            settings,
            prediction(START, 0.5),
            None,
            (),
            {"pv0": prediction(START, 0.4).energies},
            {},
            START,
        )
        assert chosen["pv0"][0] == expected
        if requested == "ml":
            assert "unavailable" in statuses["pv0"]["issue"]


def test_normalized_ha_boundary_rejects_changed_associations_and_credentials():
    """Current Energy metadata, bounded steps and unknown issuance are validated before persistence."""
    value = provider()
    metadata = document(value.configuration)
    suggestions = [{"entity_id": "sensor.pv0", "providers": [metadata], "issue": None}]
    raw = {
        "adapter": "forecast_solar",
        "entry_id": "entry",
        "configuration": metadata,
        "steps": document(value)["steps"],
        "provider_issued_at": None,
        "provider_refreshed_at": START.isoformat(),
        "configuration_revision": "configuration-a",
    }
    body = {
        "observed_at": START.isoformat(),
        "groups": [{"entity_id": "sensor.pv0", "providers": [raw]}],
    }
    assert parse_ha_predictions(body, suggestions, START)["sensor.pv0"] == (value,)
    raw["api_key"] = "must-not-enter-archive"
    with pytest.raises(ValueError):
        parse_ha_predictions(body, suggestions, START)
    raw.pop("api_key")
    raw["entry_id"] = "another"
    with pytest.raises(ValueError):
        parse_ha_predictions(body, suggestions, START)


def test_first_observation_is_immutable_across_cache_reads(tmp_path):
    """Rereading an unchanged cache cannot replace the original availability timestamp or provider issue time."""
    store = make_store(tmp_path, SolarStore)
    runtime = SolarRuntime(store)
    first = provider(refreshed=False)
    runtime.save_ha({"sensor.pv0": (first,)})
    later = replace(first, observed_at=START + timedelta(minutes=30))
    assert later.version == first.version
    runtime.save_ha({"sensor.pv0": (later,)})
    original = store.documents.read("ha-first-" + first.version + ".json")
    assert original["observed_at"] == str(START)
    assert original["provider_issued_at"] is None


def test_runtime_selects_manual_ha_archives_provenance_and_falls_back_on_expiry(tmp_path):
    """The real worker applies HA AC once, retains original evidence and cannot rejuvenate expired provider data."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    configuration = replace(
        configuration, sources=(replace(configuration.sources[0], forecast_source="ha"),)
    )
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather())
    predictions = {"sensor.pv0": (provider(),)}
    runtime.save_ha(predictions)
    runtime.receive_ha(predictions)
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert runtime.active_model("pv0") == "ha-production-v1"
    first = runtime.forecast
    assert first.energies[0].ac_kwh == 0.1
    assert first.energies[0].dc_kwh * 0.96 == pytest.approx(0.1)
    retained = store.documents.read("forecast-" + first.version + ".json")
    assert retained["ha_references"] == {"sensor.pv0": [provider().version]}
    now = START + timedelta(hours=2)
    runtime.set_weather(SITE, weather(now))
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.active_model("pv0") == "pvwatts-v2"
    assert "unavailable" in runtime.source_selection["pv0"]["issue"]
    assert store.documents.read("forecast-" + first.version + ".json") == retained


@pytest.mark.parametrize("unrelated_provider_refresh", [None, START - timedelta(hours=2)])
def test_explicit_ha_forecast_does_not_require_pvwatts_weather(
    tmp_path, unrelated_provider_refresh
):
    """A complete fresh explicit HA source remains usable when the independent weather service is unavailable."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    store.save(
        replace(configuration, sources=(replace(configuration.sources[0], forecast_source="ha"),))
    )
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, ())
    unrelated_provider = replace(
        provider(),
        configuration=replace(provider().configuration, entry_id="other"),
        configuration_revision="other-configuration",
        provider_refreshed_at=unrelated_provider_refresh,
    )
    runtime.receive_ha({"sensor.pv0": (provider(),), "sensor.other": (unrelated_provider,)})
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert runtime.forecast.weather_version == "none"
    assert runtime.active_model("pv0") == "ha-production-v1"
    assert runtime.forecast.energies[0].ac_kwh == 0.1
    assert runtime.forecast.valid_until == START + timedelta(hours=2)


@pytest.mark.parametrize("missing_hour_index", [None, 2])
def test_short_ha_forecast_keeps_its_real_end_and_rejects_a_gap(tmp_path, missing_hour_index):
    """Six covered hours compile to six hours and 2.5 kWh DC. A missing middle hour blocks admission."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    store.save(
        replace(configuration, sources=(replace(configuration.sources[0], forecast_source="ha"),))
    )
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, ())
    prediction = provider(missing=missing_hour_index)
    forecast_end = START + timedelta(hours=6)
    prediction = replace(
        prediction, steps=tuple(step for step in prediction.steps if step.start < forecast_end)
    )
    runtime.receive_ha({"sensor.pv0": (prediction,)})
    if missing_hour_index is not None:
        with pytest.raises(ValueError, match="HA solar forecast unavailable"):
            runtime.build(runtime.job(START))
        return
    assert runtime.install(runtime.build(runtime.job(START)), START)
    basis_source, measurement_configuration, report = forecast_basis(START, runtime)
    basis = basis_source.current(measurement_configuration, START, report=report)
    assert len(basis.problem.intervals) == 24
    last_interval = basis.problem.intervals[-1]
    assert last_interval.start + timedelta(hours=last_interval.duration_h) == forecast_end
    assert sum(interval.duration_h for interval in basis.problem.intervals) == 6
    assert sum(interval.load_kwh for interval in basis.problem.intervals) == 6
    assert sum(interval.pv_kwh for interval in basis.problem.intervals) == pytest.approx(2.5)


def test_provider_expiry_is_enforced_between_quarters(tmp_path):
    """A provider expiry at 00:07 blocks admission and requests fallback before the 00:15 model round."""
    from open_energy_planner.solar.basis import forecast_usable

    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    store.save(
        replace(configuration, sources=(replace(configuration.sources[0], forecast_source="ha"),))
    )
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather())
    value = replace(provider(), provider_refreshed_at=START - timedelta(hours=1, minutes=53))
    runtime.receive_ha({"sensor.pv0": (value,)})
    assert runtime.install(runtime.build(runtime.job(START)), START)
    assert forecast_usable(
        runtime.configuration, runtime.forecast, SITE, START + timedelta(minutes=7, microseconds=-1)
    )
    assert not forecast_usable(
        runtime.configuration, runtime.forecast, SITE, START + timedelta(minutes=7)
    )
    now = START + timedelta(minutes=7)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.active_model("pv0") == "pvwatts-v2"


@pytest.mark.parametrize("requested", ["automatic", "ml"])
@pytest.mark.parametrize(
    "challenger_kwh,expected", [(0.4, "ml"), (0.497, "pvwatts"), (0.7, "pvwatts")]
)
@pytest.mark.parametrize("hybrid", [False, True])
def test_tree_activation_needs_its_own_forward_days_and_material_gain(
    tmp_path, requested, challenger_kwh, expected, hybrid
):
    """Three frozen future days admit 0.4 kWh against 0.4 actual, but reject weak/worse alternatives."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    configuration = replace(
        configuration,
        sources=(replace(configuration.sources[0], forecast_source=requested),),
        inverters=(replace(configuration.inverters[0], hybrid=hybrid),),
    )
    fitted = (SimpleNamespace(source_id="pv0"),)
    history = EnergyHistory(
        "sensor.pv0",
        tuple(
            EnergyHour(START + timedelta(hours=index), index * 0.4, index * 0.4)
            for index in range(97)
        ),
    )
    for day in range(3):
        now = START + timedelta(days=day)
        choose_sources(
            store, configuration, prediction(now, 0.5), prediction(now, 0.4), fitted, {}, {}, now
        )
    now = START + timedelta(days=4, hours=1)
    policies = {"pv0": "tree-policy-a"}
    chosen, status = choose_sources(
        store,
        configuration,
        prediction(now, 0.5),
        prediction(now, challenger_kwh),
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies=policies,
    )
    assert chosen["pv0"][0] == "pvwatts"
    assert status["pv0"]["complete_days"] == 0
    assert "collecting" in status["pv0"]["issue"]
    for day in range(3):
        captured_at = START + timedelta(days=day)
        choose_sources(
            store,
            configuration,
            prediction(captured_at, 0.5),
            prediction(captured_at, challenger_kwh),
            fitted,
            {},
            {},
            captured_at,
            ml_policies=policies,
        )
    chosen, status = choose_sources(
        store,
        configuration,
        prediction(now, 0.5),
        prediction(now, challenger_kwh),
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies=policies,
    )
    assert chosen["pv0"][0] == expected
    assert status["pv0"]["complete_days"] == 3
    assert status["pv0"]["mae_kwh"]["ml"] == pytest.approx(abs(challenger_kwh - 0.4))
    changed, changed_status = choose_sources(
        store,
        configuration,
        prediction(now, 0.5),
        prediction(now, challenger_kwh),
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies={"pv0": "tree-policy-b"},
    )
    assert changed["pv0"][0] == "pvwatts"
    assert changed_status["pv0"]["complete_days"] == 0


@pytest.mark.parametrize(
    "requested,include_ha,expected",
    [("automatic", True, "ha"), ("ml", True, "pvwatts"), ("automatic", False, "pvwatts")],
)
def test_eligible_ha_when_tree_fails_material_gain(tmp_path, requested, include_ha, expected):
    """On the same 72 hours, Automatic retains HA at .098 MAE when ML at .096 fails the .005 gain."""
    store = make_store(tmp_path, SolarStore)
    configuration = plant()
    configuration = replace(
        configuration, sources=(replace(configuration.sources[0], forecast_source=requested),)
    )
    fitted = (SimpleNamespace(source_id="pv0"),)
    policies = {"pv0": "tree-policy-a"}
    for day in range(3):
        now = START + timedelta(days=day)
        choose_sources(
            store,
            configuration,
            prediction(now, 0.5),
            prediction(now, 0.496),
            fitted,
            {"pv0": prediction(now, 0.498).energies} if include_ha else {},
            {},
            now,
            ml_policies=policies,
        )
    now = START + timedelta(days=4, hours=1)
    history = EnergyHistory(
        "sensor.pv0",
        tuple(
            EnergyHour(START + timedelta(hours=index), index * 0.4, index * 0.4)
            for index in range(97)
        ),
    )
    chosen, status = choose_sources(
        store,
        configuration,
        prediction(now, 0.5),
        prediction(now, 0.496),
        fitted,
        {"pv0": prediction(now, 0.498).energies} if include_ha else {},
        {"sensor.pv0": history},
        now,
        ml_policies=policies,
    )
    assert chosen["pv0"][0] == expected
    assert status["pv0"]["complete_days"] == 3
    assert status["pv0"]["mae_kwh"]["pvwatts"] == pytest.approx(0.100)
    assert status["pv0"]["mae_kwh"]["ml"] == pytest.approx(0.096)
    if include_ha:
        assert status["pv0"]["mae_kwh"]["ha"] == pytest.approx(0.098)
    if expected == "ha":
        assert status["pv0"]["issue"] is None
