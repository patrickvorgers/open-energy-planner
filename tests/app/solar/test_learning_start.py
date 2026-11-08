"""Keep explicit production-regime boundaries in learning without deleting earlier evidence."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.solar.learning import learning_interval
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.source_selection import family
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import configuration_document, parse_configuration
from tests.app.solar.support import SITE, plant, weather
from tests.app.solar.test_boosting import START, training_data
from tests.database.support.archive import make_store


def test_default_learning_boundary_preserves_original_configuration_hash_and_wire_shape():
    """Leaving the new date empty does not create a new definition or add an empty archive field."""
    original = {
        "sources": [
            {
                "source_id": "pv0",
                "entity_id": "sensor.pv0",
                "inverter_id": "i0",
                "capacity_w": 1000.0,
                "orientation": "south",
                "tilt": 0.0,
                "measurement_boundary": "ac",
                "can_curtail": False,
            }
        ],
        "inverters": [
            {"inverter_id": "i0", "ac_limit_w": 5000.0, "efficiency": 0.96, "hybrid": False}
        ],
    }
    expected = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    configuration = plant()
    assert configuration.version == expected
    assert "learning_start" not in configuration_document(configuration)["sources"][0]


def test_learning_boundary_uses_exact_utc_instant_and_round_trips_through_the_active_store(
    tmp_path,
):
    """Amsterdam midnight in summer is retained as the preceding UTC day's 22:00 boundary."""
    configuration = plant()
    source = replace(configuration.sources[0], learning_start="2026-04-01T00:00:00+02:00")
    configured = replace(configuration, sources=(source,))
    assert source.learning_start == "2026-03-31T22:00:00+00:00"
    assert configured.version != configuration.version
    assert parse_configuration(configuration_document(configured)) == configured
    store = make_store(tmp_path, SolarStore)
    store.save(configured)
    assert SolarStore(store.documents).current == configured


@pytest.mark.parametrize(
    "value", ["invalid-date", "2026-04-01", "2026-04-01T00:00:00", 0, True, {}]
)
def test_learning_boundary_rejects_missing_timezone_and_malformed_values(value):
    """A learning date must identify an aware instant rather than a guessed browser-local time."""
    with pytest.raises(ValueError, match="Invalid solar learning start"):
        replace(plant().sources[0], learning_start=value)


def test_learning_interval_keeps_the_boundary_hour_but_rejects_earlier_or_straddling_hours():
    """No interval crossing a regime boundary is admitted, while the first complete later hour is usable."""
    source = replace(plant().sources[0], learning_start="2026-04-01T00:00:00+00:00")
    assert not learning_interval(source, START - timedelta(hours=1), START)
    assert not learning_interval(
        source, START - timedelta(minutes=30), START + timedelta(minutes=30)
    )
    assert learning_interval(source, START, START + timedelta(hours=1))
    assert not learning_interval(source, START, START)


def test_changing_one_source_boundary_starts_a_new_family_only_for_that_source():
    """A deliberate regime change invalidates its learned evidence without resetting an unaffected neighbor."""
    original = plant(count=2)
    changed_source = replace(original.sources[0], learning_start=START.isoformat())
    changed = replace(original, sources=(changed_source, original.sources[1]))
    assert family(original, original.sources[0], SITE) != family(changed, changed.sources[0], SITE)
    assert family(original, original.sources[1], SITE) == family(changed, changed.sources[1], SITE)
    equivalent = replace(changed_source, learning_start="2026-04-01T02:00:00+02:00")
    assert equivalent == changed_source
    assert family(changed, equivalent, SITE) == family(changed, changed_source, SITE)


def test_training_and_final_validation_both_respect_the_learning_boundary(monkeypatch):
    """Only the 35 older post-change days train the model and all seven later validation days remain independent."""
    from open_energy_planner.solar import boosting

    configuration = plant()
    history, physical, conditions, before = training_data()
    original_history = deepcopy(history)
    cutoff = START + timedelta(days=14)
    source = replace(configuration.sources[0], learning_start=cutoff.isoformat())
    train = boosting._train
    fitted = []

    def record(rows):
        """Capture the actual learner input while executing the unchanged native training adapter."""
        fitted.extend(rows)
        return train(rows)

    monkeypatch.setattr(boosting, "_train", record)
    result = boosting.fit_boosted_model(
        source,
        configuration.inverters[0],
        SITE,
        history,
        physical,
        conditions,
        before,
    )
    assert history == original_history
    assert result.summary.training_days == 35
    assert result.summary.validation_days == 7
    assert result.summary.excluded_counts["before_learning_start"] == 336
    assert fitted
    assert min(row[0] for row in fitted) >= cutoff
    assert max(row[0] for row in fitted) < before - timedelta(days=7)
    assert result.model is not None
    late = replace(source, learning_start=(before - timedelta(days=2)).isoformat())
    result = boosting.fit_boosted_model(
        late,
        configuration.inverters[0],
        SITE,
        history,
        physical,
        conditions,
        before,
    )
    assert result.summary.training_hours == 0
    assert result.summary.validation_days == 2
    assert result.model is None
    assert history == original_history


def test_learning_boundary_change_revokes_an_old_candidate_and_queued_model(tmp_path):
    """A saved source boundary clears the active candidate and rejects a worker using the prior definition."""
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    now = START + timedelta(days=56)
    runtime.set_weather(SITE, weather(now))
    queued = runtime.job(now)
    runtime.candidate = object()
    runtime.calibrations = (object(),)
    runtime.source_selection = {"pv0": {"active": "ml"}}
    changed = replace(
        store.current,
        sources=(replace(store.current.sources[0], learning_start=START.isoformat()),),
    )
    store.save(changed)
    runtime.configure(changed)
    assert runtime.candidate is None
    assert runtime.calibrations == ()
    assert runtime.source_selection == {}
    assert not runtime.install((queued[0],), now)
    assert SolarRuntime(SolarStore(store.documents)).configuration == changed


def test_accuracy_cohort_cannot_use_measured_hours_before_the_learning_boundary(tmp_path):
    """Only the last of three otherwise complete captured HA days lies inside the selected production regime."""
    from types import SimpleNamespace

    from open_energy_planner.history.model import EnergyHistory, EnergyHour
    from open_energy_planner.solar.source_selection import choose_sources
    from tests.app.solar.test_source_selection import prediction

    configuration = plant()
    boundary = START + timedelta(days=3, hours=-2)
    configuration = replace(
        configuration,
        sources=(replace(configuration.sources[0], learning_start=boundary.isoformat()),),
    )
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    fitted = (SimpleNamespace(source_id="pv0"),)
    for index in range(3):
        now = START + timedelta(days=index)
        choose_sources(
            store,
            configuration,
            prediction(now, 0.5, configuration=configuration),
            prediction(now, 0.4, configuration=configuration, model="solar-xgboost-production-v2"),
            fitted,
            {},
            {},
            now,
            ml_policies={"pv0": "a" * 64},
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
        prediction(now, 0.5, configuration=configuration),
        prediction(now, 0.4, configuration=configuration, model="solar-xgboost-production-v2"),
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies={"pv0": "a" * 64},
    )
    assert status["pv0"]["complete_days"] == 1
    assert status["pv0"]["mae_kwh"] == {}
    assert chosen["pv0"][0] == "pvwatts"
