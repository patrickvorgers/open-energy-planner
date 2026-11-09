"""Prove individual forecasts preserve household accounting and remain governed by decision evidence."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.appliances.forecasting import (
    ConsumerForecasts,
    build_components,
    parse_selection,
)
from open_energy_planner.demand.calculation.model import TemperatureHour
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from tests.app.demand.test_demand import START, channel, config
from tests.database.support.archive import make_store


def catalogue():
    """Declare a measured heat pump and its containing circuit, independent of history availability."""
    return {
        "entities": [{"entity_id": "sensor.heat", "device_name": "Heat pump"}],
        "suggestions": {
            "consumers": [
                {"entity_id": "sensor.heat", "upstream": "sensor.circuit"},
                {"entity_id": "sensor.circuit", "upstream": None},
            ]
        },
    }


@pytest.mark.parametrize(
    "failure_stage", ["partial_device", "all_devices", "residual", "composition"]
)
def test_component_failure_details_never_publish_private_exception_values(
    tmp_path, monkeypatch, failure_stage
):
    """All failure exits retain useful status without exposing raw errors or opaque IDs in detail."""
    from open_energy_planner.appliances import forecasting

    consumers = ConsumerForecasts(make_store(tmp_path, DemandStore).documents)
    private_entity = "sensor.synthetic_private_device"
    consumers.sync(
        {
            "entities": [{"entity_id": entity} for entity in ("sensor.ready", private_entity)],
            "suggestions": {
                "consumers": [
                    {"entity_id": entity, "upstream": None}
                    for entity in ("sensor.ready", private_entity)
                ]
            },
        }
    )
    consumers.save(
        parse_selection(
            [
                {"entity_id": entity, "weather_sensitive": False}
                for entity in ("sensor.ready", private_entity)
            ]
        ),
        consumers.version,
    )
    for entity in ("sensor.ready", private_entity):
        consumers.histories[entity] = channel(entity, [0.1] * (35 * 24))
    original_component = forecasting.component
    sentinel = "synthetic-private-error-password-location"

    def fail_selected_component(configuration, *arguments, **keywords):
        """Inject private failures at each responsible component boundary."""
        residual = keywords.get("boundary") is not None
        if (
            failure_stage == "residual"
            and residual
            or failure_stage == "all_devices"
            and not residual
            or failure_stage == "partial_device"
            and configuration.entities == (private_entity,)
        ):
            raise ValueError(sentinel)
        return original_component(configuration, *arguments, **keywords)

    def fail_composition(*_arguments, **_keywords):
        """Reject only final composition without altering component model calculations."""
        raise ValueError(sentinel)

    monkeypatch.setattr(forecasting, "component", fail_selected_component)
    if failure_stage == "composition":
        monkeypatch.setattr(forecasting, "DemandForecast", fail_composition)
    _, consumers.result = build_components(
        config(),
        (channel("sensor.house", [0.5] * (35 * 24)),),
        (),
        START + timedelta(days=35),
        consumers.inputs(),
    )
    public = consumers.document()
    assert sentinel not in json.dumps(public)
    assert private_entity not in public["result"]["detail"]
    assert public["result"]["detail"] == (
        "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
    )


@pytest.mark.parametrize(
    "control_case", ["unavailable_device", "separated_device", "global_wildcard"]
)
def test_controlled_day_exclusions_follow_the_actual_residual_measurement_boundary(
    tmp_path, control_case
):
    """Real forward weather comparisons exclude controlled remainder, but not an already separated meter."""
    from tests.app.forecast.support.weather_learning import history, known_weather

    samples, weather = history()
    separate = control_case != "unavailable_device"
    control_store = make_store(tmp_path / "controlled", DemandStore)
    ordinary_store = make_store(tmp_path / "ordinary", DemandStore)
    consumers = ConsumerForecasts(control_store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}]),
        consumers.version,
    )
    if separate:
        consumers.histories["sensor.heat"] = channel(
            "sensor.heat", [row.residual_kwh for row in samples]
        )
    household = channel(
        "sensor.house", [row.residual_kwh + (0.3 if separate else 0) for row in samples]
    )
    controlled_days = (
        {"household": ["*"]}
        if control_case == "global_wildcard"
        else {
            "sensor.heat": [
                (START + timedelta(days=day)).date().isoformat() for day in range(40, 49)
            ]
        }
    )
    for day in range(42, 47):
        now = START + timedelta(days=day)
        temperatures = known_weather(weather[: (day + 3) * 24], now)
        _, ordinary = build_components(
            replace(config(), timezone="UTC"),
            (household,),
            temperatures,
            now,
            consumers.inputs(),
            documents=ordinary_store.documents,
        )
        _, controlled = build_components(
            replace(config(), timezone="UTC"),
            (household,),
            temperatures,
            now,
            consumers.inputs(),
            documents=control_store.documents,
            controlled_days=controlled_days,
        )
    if control_case == "unavailable_device":
        assert ordinary["residual_selection"]["selected"] == "weather"
        assert controlled["residual_selection"]["selected"] == "baseline"
        assert controlled["residual_selection"]["completed_windows"] == 0
    elif control_case == "separated_device":
        assert ordinary["devices"][0]["model_selection"]["selected"] == "weather"
        assert controlled["devices"][0]["model_selection"]["selected"] == "baseline"
        assert controlled["residual_selection"]["completed_windows"] == 3
    else:
        assert ordinary["devices"][0]["model_selection"]["selected"] == "weather"
        assert controlled["devices"][0]["model_selection"]["selected"] == "baseline"
        assert controlled["residual_selection"]["completed_windows"] == 0


def test_consumer_selection_preserves_versions_and_rejects_overlap(tmp_path):
    """Only Energy-owned disjoint meters can be selected. Choices survive reopening the same archive."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    selection = parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}])
    original = consumers.version
    consumers.save(selection, original)
    assert ConsumerForecasts(store.documents).selected == selection
    with pytest.raises(ValueError, match="Refresh"):
        consumers.save((), original)
    with pytest.raises(ValueError, match="overlapping"):
        consumers.save(
            parse_selection(
                [*selection, {"entity_id": "sensor.circuit", "weather_sensitive": False}]
            ),
            consumers.version,
        )
    with pytest.raises(ValueError, match="Home Assistant Energy"):
        consumers.save(
            parse_selection([{"entity_id": "sensor.unknown", "weather_sensitive": False}]),
            consumers.version,
        )


def test_composite_retains_measured_baseline_until_forward_accuracy_is_available(tmp_path):
    """Known .3-kWh residual and measured device profiles sum once before weather candidates have forward proof."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}]),
        consumers.version,
    )
    hours = 35 * 24
    temperatures = [5 + (index // 24) % 12 for index in range(hours)]
    heating = [(20 - temperature) * 0.05 for temperature in temperatures]
    household = channel("sensor.house", [value + 0.3 for value in heating])
    consumers.histories["sensor.heat"] = channel("sensor.heat", heating)
    now = START + timedelta(hours=hours)
    history_weather = tuple(
        TemperatureHour(START + timedelta(hours=index), value, START)
        for index, value in enumerate(temperatures)
    )
    forecasts = []
    for future_temperature in (0, 20):
        future = tuple(
            TemperatureHour(now + timedelta(hours=index), future_temperature, now)
            for index in range(49)
        )
        total, result = build_components(
            config(), (household,), history_weather + future, now, consumers.inputs()
        )
        assert total is not None, result
        assert result["devices"][0]["forecast"]["model"] == "weekday-hour-v2"
        for index, step in enumerate(total.steps):
            separate = result["devices"][0]["forecast"]["steps"][index]["kwh"]
            residual = result["residual"]["steps"][index]["kwh"]
            assert residual == pytest.approx(0.3 * step.duration_h, abs=0.005)
            assert step.kwh == pytest.approx(separate + residual)
        forecasts.append(sum(step.kwh for step in total.steps))
    assert forecasts[0] == pytest.approx(forecasts[1])


def test_missing_device_history_keeps_existing_household_forecast(tmp_path):
    """A selected unmeasured device cannot remove demand or activate a zero startup consumer prediction."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    runtime.consumers.sync(catalogue())
    runtime.consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}]),
        runtime.consumers.version,
    )
    runtime.histories["sensor.house"] = channel("sensor.house", [0.4] * (35 * 24))
    runtime.data_versions["sensor.house"] = "fixture"
    now = START + timedelta(days=35)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "weekday-hour-v2"
    assert runtime.candidate is None
    assert runtime.consumers.result["status"] == "history_or_weather_not_ready"


@pytest.mark.parametrize("ready_first", [True, False])
def test_short_device_history_retains_independent_forecasts_and_unmodelled_load(
    tmp_path, ready_first
):
    """A device with 59 hours stays in residual demand while a device with 35 days remains forecastable."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    ready_id = "sensor.a_ready" if ready_first else "sensor.z_ready"
    short_id = "sensor.m_short"
    metadata = {
        "entities": [{"entity_id": entity} for entity in (ready_id, short_id)],
        "suggestions": {
            "consumers": [
                {"entity_id": entity, "upstream": None} for entity in (ready_id, short_id)
            ]
        },
    }
    runtime.consumers.sync(metadata)
    runtime.consumers.save(
        parse_selection(
            [{"entity_id": entity, "weather_sensitive": False} for entity in (ready_id, short_id)]
        ),
        runtime.consumers.version,
    )
    hours = 35 * 24
    household = channel("sensor.house", [0.5] * hours)
    runtime.histories["sensor.house"] = household
    runtime.data_versions["sensor.house"] = "fixture"
    runtime.consumers.histories[ready_id] = channel(ready_id, [0.2] * hours)
    short = channel(short_id, [0.1] * hours)
    runtime.consumers.histories[short_id] = replace(short, hours=short.hours[-60:])
    now = START + timedelta(hours=hours)
    candidate, result = build_components(
        config(), (household,), (), now, runtime.consumers.inputs()
    )
    assert candidate.model == "consumer-composite-v1"
    assert result["status"] == "partial_history"
    assert (
        result["detail"]
        == "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
    )
    assert [record["entity_id"] for record in result["devices"]] == [ready_id]
    assert result["devices"][0]["status"] == "forward_archive_required"
    parameters = dict(json.loads(json.dumps(result["model_parameters"], allow_nan=False)))
    assert set(parameters) == {ready_id, "residual"}
    assert parameters[ready_id] and parameters["residual"]
    measured = result["devices"][0]["baseline_forecast"]["steps"]
    remaining = result["residual_baseline"]["steps"]
    assert len(measured) == len(remaining) > 0
    for component_step, residual_step in zip(measured, remaining, strict=True):
        duration_hours = component_step["duration_h"]
        assert component_step["start"] == residual_step["start"]
        assert component_step["kwh"] == pytest.approx(0.2 * duration_hours)
        # The 0.1-kWh/h device remains alongside the 0.2-kWh/h unmetered load.
        assert residual_step["kwh"] == pytest.approx((0.1 + 0.2) * duration_hours)
        assert component_step["kwh"] + residual_step["kwh"] == pytest.approx(0.5 * duration_hours)
    assert result["residual"]["steps"]
    runtime.select_learned(True)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.candidate.model == "consumer-composite-v1"
    assert runtime.forecast.model == "consumer-composite-v1"
    assert runtime.consumers.result["status"] == "partial_history"
    path = "quality-components-" + runtime.forecast.version + ".json"
    original = store.documents.read(path)
    assert original["selected"] is True
    original_components = store.documents.read(
        "consumer-forecast-" + original["version"] + ".json"
    )["components"]
    assert original_components["devices"][0]["forecast"] == json.loads(
        json.dumps(result["devices"][0]["forecast"])
    )
    assert original_components["model_parameters"]
    assert store.documents.exists("consumer-forecast-" + runtime.forecast.version + ".json")
    runtime.consumers.histories[ready_id] = channel(ready_id, [0.4] * hours)
    later = now + timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert DemandStore(store.documents).documents.read(path) == original


def test_invalid_residual_retains_valid_device_records_without_enabling_replacement(tmp_path):
    """A complete component cannot authorize replacement when household evidence has only 59 hours."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": False}]),
        consumers.version,
    )
    hours = 35 * 24
    consumers.histories["sensor.heat"] = channel("sensor.heat", [0.2] * hours)
    household = channel("sensor.house", [0.5] * hours)
    household = replace(household, hours=household.hours[-60:])
    candidate, result = build_components(
        config(), (household,), (), START + timedelta(hours=hours), consumers.inputs()
    )
    assert candidate is None
    assert result["status"] == "history_or_weather_not_ready"
    assert (
        result["detail"]
        == "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
    )
    assert [record["entity_id"] for record in result["devices"]] == ["sensor.heat"]
    assert result["devices"][0]["status"] == "forward_archive_required"
    parameters = dict(json.loads(json.dumps(result["model_parameters"], allow_nan=False)))
    assert set(parameters) == {"sensor.heat"}
    assert parameters["sensor.heat"]
    assert result["devices"][0]["baseline_forecast"]["steps"]
    assert "residual" not in result and "residual_baseline" not in result


def test_removed_consumer_and_failed_history_do_not_become_zero(tmp_path):
    """Unavailable history advances collection to other meters. Removed Energy membership blocks composition."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": False}]),
        consumers.version,
    )
    now = START + timedelta(days=35)
    request = consumers.request(config(), now)
    consumers.receive(request, error="history_unavailable", now=now)
    assert consumers.request(config(), now) is None
    assert consumers.failures["sensor.heat"] == "history_unavailable"
    consumers.sync({"entities": [], "suggestions": {"consumers": []}})
    forecast, result = build_components(config(), (), (), now, consumers.inputs())
    assert forecast is None and result["status"] == "energy_consumer_removed"


@pytest.mark.parametrize("bad_hours", [1, 24])
@pytest.mark.parametrize("excess_kwh", [0.001, 0.01])
def test_residual_filter_retains_components_without_hiding_substantial_conflicts(
    tmp_path, bad_hours, excess_kwh
):
    """Residual rounding is usable, isolated conflicts are omitted, and enough usable days retain the composite."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": False}]),
        consumers.version,
    )
    consumers.histories["sensor.heat"] = channel(
        "sensor.heat", [1 + excess_kwh] * bad_hours + [0.2] * (240 - bad_hours)
    )
    candidate, result = build_components(
        config(),
        (channel("sensor.house", [1] * 240),),
        (),
        START + timedelta(hours=240),
        consumers.inputs(),
    )
    assert [record["entity_id"] for record in result["devices"]] == ["sensor.heat"]
    assert candidate is not None
    assert candidate.excluded_devices == ()
    for step, residual, device in zip(
        candidate.steps,
        result["residual"]["steps"],
        result["devices"][0]["forecast"]["steps"],
        strict=True,
    ):
        assert step.kwh == pytest.approx(residual["kwh"] + device["kwh"])
    assert result["residual"]["model"] == "weekday-hour-v2"


def test_runtime_separation_uses_baselines_and_archives_exact_components(tmp_path):
    """Complete measured separation is active immediately while learned models await independent daily proof."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    runtime.consumers.sync(catalogue())
    runtime.consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": False}]),
        runtime.consumers.version,
    )
    runtime.histories["sensor.house"] = channel("sensor.house", [0.5] * (35 * 24))
    runtime.consumers.histories["sensor.heat"] = channel("sensor.heat", [0.2] * (35 * 24))
    runtime.data_versions["sensor.house"] = "fixture"
    now = START + timedelta(days=35)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.candidate.model == "consumer-composite-v1"
    import json

    assert json.loads(json.dumps(runtime.consumers.document()))["result"]["devices"]
    assert runtime.forecast.model == "consumer-composite-v1"
    assert (
        store.documents.read("consumer-forecast-" + runtime.candidate.version + ".json")[
            "components"
        ]["devices"][0]["entity_id"]
        == "sensor.heat"
    )
    original_path = "consumer-forecast-" + runtime.candidate.version + ".json"
    original_components = store.documents.read(original_path)
    runtime.select_learned(True)
    assert runtime.job(now) is None
    assert runtime.forecast.model == "consumer-composite-v1"
    assert runtime.consumers.result["devices"][0]["status"] == "collecting_forward_accuracy"
    assert store.documents.read(original_path) == original_components
    runtime.consumers.histories.clear()
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "weekday-hour-v2"


def test_empty_household_boundary_retains_readiness_status(tmp_path):
    """Selected devices cannot turn an annual-estimate-only household into a failed forecast worker."""
    store = make_store(tmp_path, DemandStore)
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue())
    consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": False}]),
        consumers.version,
    )
    forecast, result = build_components(
        replace(config(), terms=()), (), (), START + timedelta(days=35), consumers.inputs()
    )
    assert forecast is None and result["status"] == "household_boundary_missing"
    consumers.result = {
        "status": "fixture",
        "model_parameters": {"private": "coefficients"},
        "devices": [],
    }
    assert "model_parameters" not in consumers.document()["result"]
    assert consumers.result["model_parameters"] == {"private": "coefficients"}


def test_missing_weather_uses_explicit_historical_component_fallback(tmp_path):
    """Weather outages retain measured profiles inside the active one-count household composition."""
    store = make_store(tmp_path, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    runtime.consumers.sync(catalogue())
    runtime.consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}]),
        runtime.consumers.version,
    )
    runtime.histories["sensor.house"] = channel("sensor.house", [0.5] * (35 * 24))
    runtime.consumers.histories["sensor.heat"] = channel("sensor.heat", [0.2] * (35 * 24))
    runtime.data_versions["sensor.house"] = "fixture"
    now = START + timedelta(days=35)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    device = runtime.consumers.document()["result"]["devices"][0]
    assert device["weather_sensitive"] is True
    assert device["weather_features_used"] is False
    assert device["forecast"]["model"] == "weekday-hour-v2"
    assert sum(step["kwh"] for step in device["forecast"]["steps"]) == pytest.approx(9.6)
    assert runtime.forecast.model == "consumer-composite-v1"
    assert runtime.candidate.model == "consumer-composite-v1"
    assert runtime.model_status == "active"
