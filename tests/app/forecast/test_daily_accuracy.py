"""Prove demand model admission uses original matching local days, not retrospective predictions."""

from datetime import date, timedelta

import pytest

from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.forecast_selection.accuracy import choose_daily
from open_energy_planner.forecast_selection.calculation.accuracy import comparison_hours
from tests.app.forecast.support.daily_accuracy import START, forecasts, measurements
from tests.database.support.archive import make_store


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 10, 26), 24)]
)
def test_local_daily_hours_respect_dst(day, hours):
    """A calendar day contains 23, 25 or 24 comparable hourly measurements in Amsterdam."""
    starts, end = comparison_hours(day, "Europe/Amsterdam")
    assert len(starts) == hours
    assert starts[-1] + timedelta(hours=1) == end


def test_three_original_days_select_candidate_and_keep_frozen_predictions(tmp_path):
    """A .2-kWh forecast beats .5 by .3 on three complete days, including the 25-hour day."""
    store = make_store(tmp_path, DemandStore)
    result = None
    for offset in range(6):
        now = START + timedelta(days=offset)
        result = choose_daily(
            store.documents, "washer", "Europe/Amsterdam", forecasts(now), measurements(now), now
        )
        if offset < 4:
            assert result["selected"] == "baseline"
    assert result["selected"] == "candidate"
    assert result["completed_windows"] == 3
    assert all(window["mae_kwh"]["candidate"] == 0 for window in result["windows"])
    assert all(window["mae_kwh"]["baseline"] == pytest.approx(0.3) for window in result["windows"])
    versions = [window["forecast_version"] for window in result["windows"]]
    changed = choose_daily(
        store.documents,
        "washer",
        "Europe/Amsterdam",
        forecasts(now, candidate=100),
        measurements(now),
        now,
    )
    assert [window["forecast_version"] for window in changed["windows"]] == versions


def test_missing_measurements_and_controlled_days_cannot_promote_a_model(tmp_path):
    """Missing hours and planner intervention days never count as successful behavior comparisons."""
    store = make_store(tmp_path, DemandStore)
    for offset in range(6):
        now = START + timedelta(days=offset)
        result = choose_daily(
            store.documents,
            "charger",
            "Europe/Amsterdam",
            forecasts(now),
            measurements(now),
            now,
            excluded_dates=[
                (START + timedelta(days=index)).date().isoformat() for index in range(6)
            ],
        )
    assert result["selected"] == "baseline"
    assert result["completed_windows"] == 0
    assert result["excluded_reason"] == "controlled_use"
    missing = choose_daily(store.documents, "charger", "Europe/Amsterdam", forecasts(now), {}, now)
    assert missing["selected"] == "baseline"
    assert missing["completed_windows"] == 0


def test_restart_does_not_reconstruct_a_missing_day(tmp_path):
    """History without pre-midnight predictions cannot provide selection evidence after startup."""
    store = make_store(tmp_path, DemandStore)
    now = START + timedelta(days=10)
    result = choose_daily(
        store.documents, "household", "Europe/Amsterdam", forecasts(now), measurements(now), now
    )
    assert result["selected"] == "baseline"
    assert result["completed_windows"] == 0


def test_latest_pre_midnight_forecast_is_used(tmp_path):
    """An improved evening forecast replaces the morning pending capture, never a frozen day."""
    store = make_store(tmp_path, DemandStore)
    now = START
    choose_daily(store.documents, "washer", "UTC", forecasts(now, candidate=2), {}, now)
    evening = now.replace(hour=23, minute=45)
    choose_daily(store.documents, "washer", "UTC", forecasts(evening), {}, evening)
    for offset in (1, 2):
        later = evening + timedelta(days=offset)
        result = choose_daily(
            store.documents, "washer", "UTC", forecasts(later), measurements(later), later
        )
    assert result["completed_windows"] == 1
    assert result["windows"][0]["mae_kwh"]["candidate"] == 0


def test_a_later_bad_day_revokes_selection_without_rewriting_forecasts(tmp_path):
    """An admitted .2-kWh model loses to the .5-kWh baseline after actual use changes to .5."""
    documents = make_store(tmp_path, DemandStore).documents
    for offset in range(6):
        now = START + timedelta(days=offset)
        result = choose_daily(documents, "washer", "UTC", forecasts(now), measurements(now), now)
    assert result["selected"] == "candidate"
    original = list(documents.iter_prefix("accuracy-day-"))
    later = now + timedelta(days=2)
    changed = {
        instant: (0.5 if instant >= now else amount)
        for instant, amount in measurements(later).items()
    }
    result = choose_daily(documents, "washer", "UTC", forecasts(later), changed, later)
    assert result["selected"] == "baseline"
    assert result["status"] == "no_consistent_accuracy_benefit"
    assert all(record in list(documents.iter_prefix("accuracy-day-")) for record in original)


def test_corrupted_pending_forecast_is_preserved_and_cannot_promote(tmp_path):
    """Changing a retained prediction without its digest disables comparison without silent replacement."""
    documents = make_store(tmp_path, DemandStore).documents
    choose_daily(documents, "washer", "UTC", forecasts(START), {}, START)
    record = next(documents.iter_prefix("accuracy-pending-"))
    record["capture"]["predictions"]["candidate"][0] = 999
    from open_energy_planner.demand.calculation.model import digest

    family = record["capture"]["family"]
    path = "accuracy-pending-" + family + ".json"
    documents.write(path, record)
    result = choose_daily(
        documents,
        "washer",
        "UTC",
        forecasts(START + timedelta(days=1)),
        {},
        START + timedelta(days=1),
    )
    assert result["selected"] == "baseline"
    assert result["status"] == "comparison_evidence_unreadable"
    assert documents.read(path) == record
    assert record["version"] != digest(record["capture"])
