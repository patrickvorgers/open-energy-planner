"""Independent original-forecast and Recorder-evidence regressions on every supported backend."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from open_energy_planner.forecast_quality.calculation.metrics import series
from open_energy_planner.forecast_quality.daily import (
    DailyForecastArchive,
    controlled_consumers,
    daily_evidence,
    day_window,
    forecast_context,
)
from open_energy_planner.forecast_quality.evidence import evidence, measured
from open_energy_planner.forecast_quality.history import HistoryArchive
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import archive_engine
from tests.app.forecast.support.quality import START, prediction
from tests.database.support.backends import BACKENDS

SOURCE = {"entity_id": "sensor.house", "device_id": "meter", "unit": "kWh", "version": "a" * 64}


@pytest.fixture(params=BACKENDS, ids=[name for name, _url in BACKENDS])
def archive(request, tmp_path):
    """Own a disposable real backend. All durable evidence uses the shared document table."""
    backend, url = request.param
    engine = archive_engine(tmp_path) if backend == "sqlite" else create_engine(url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    try:
        yield HistoryArchive(SqlDocuments(engine, "demand"))
    finally:
        SCHEMA.drop_all(engine)
        engine.dispose()


def receipt(archive, values, *, source=SOURCE, now=START + timedelta(hours=4)):
    """Submit cumulative endpoints through normal bounded admission, never treat them as interval energy."""
    pending = archive.request(now, [source])
    assert pending is not None
    body = {
        "request_version": pending["version"],
        "entity_id": source["entity_id"],
        "unit": "kWh",
        "hours": [
            [(START + timedelta(hours=offset)).timestamp(), total, max(0, total)]
            for offset, total in values
        ],
    }
    archive.receive(pending, body, now)
    return pending


def test_midnight_context_uses_current_archived_refs_without_mutating_original_plan():
    """Freeze the cached house reference and explicitly missing PV/component identities. Updating labels must not alter an original plan."""
    from types import SimpleNamespace

    household = SimpleNamespace(
        configuration_version="house",
        version="house-original",
        model="weekday-hour-v2",
        issued_at=START,
    )
    demand = SimpleNamespace(
        configuration=SimpleNamespace(version="house", entities=("sensor.house",)),
        forecast=household,
        consumers=SimpleNamespace(selected=[{"entity_id": "sensor.heat"}]),
    )
    solar = SimpleNamespace(
        configuration=SimpleNamespace(
            version="solar", sources=(SimpleNamespace(source_id="roof", entity_id="sensor.pv"),)
        ),
        forecast=None,
    )
    catalogue = {
        "entities": [
            {"entity_id": identity, "unit": "kWh", "device_id": identity, "name": title}
            for identity, title in (
                ("sensor.house", "House"),
                ("sensor.heat", "Heat pump"),
                ("sensor.pv", "South roof"),
            )
        ]
    }
    original = {
        "comparison_names": {"roof": "Old roof label"},
        "forecast_quality_controlled": ["sensor.heat"],
    }
    retained = deepcopy(original)
    context = forecast_context(demand, solar, catalogue, original)
    assert context["input_snapshot"]["demand_evidence"][0]["forecast_version"] == "house-original"
    assert context["input_snapshot"]["solar_evidence"] == []
    assert context["comparison_names"]["roof"] == "South roof"
    assert {source["id"] for source in context["expected_sources"]} == {
        "household",
        "sensor.heat",
        "roof",
    }
    context["forecast_quality_controlled"].clear()
    assert original == retained


def daily_fixture(archive, day="2026-10-26", timezone="Europe/Amsterdam", *, missing=False):
    """Freeze .25-kWh quarters before midnight and retain independent 1.25-kWh hourly actuals over the local day."""
    start, end = day_window(day, timezone)
    hours = int((end - start).total_seconds() / 3600)
    forecast = {
        "model": "weekday-hour-v2",
        "version": "daily-original",
        "issued_at": (start - timedelta(minutes=5)).isoformat(),
        "steps": [
            {
                "start": (start + timedelta(minutes=15 * interval_index)).isoformat(),
                "duration_h": 0.25,
                "kwh": 0.25,
            }
            for interval_index in range(hours * 4)
        ],
    }
    archive.documents.write("forecast-daily-original.json", {"forecast": forecast})
    archive.documents.write(
        "configuration-daily-house.json",
        {"terms": [{"entity_id": "sensor.house", "sign": 1}], "exclusions": []},
    )
    context = {
        "forecast_quality_sources": [SOURCE],
        "input_snapshot": {
            "demand_evidence": [
                {
                    "forecast_version": "daily-original",
                    "configuration_version": "daily-house",
                    "model": "weekday-hour-v2",
                    "issued_at": forecast["issued_at"],
                }
            ],
            "solar_evidence": [],
        },
    }
    daily = DailyForecastArchive(archive.documents)
    daily.observe(start - timedelta(seconds=3), timezone, context)
    daily.observe(start + timedelta(seconds=2), timezone, context)
    now = end + timedelta(minutes=10)
    pending = archive.request(now, [SOURCE])
    archive.receive(
        pending,
        {
            "request_version": pending["version"],
            "entity_id": SOURCE["entity_id"],
            "unit": "kWh",
            "hours": [
                [
                    (start + timedelta(hours=hour_index - 1)).timestamp(),
                    100 + 1.25 * hour_index,
                    100 + 1.25 * hour_index,
                ]
                for hour_index in range(hours + 1)
                if not (missing and hour_index == 5)
            ],
        },
        now,
    )
    return daily, daily.read(day, timezone), now, context


@pytest.mark.parametrize("day, hours", [("2026-03-29", 23), ("2026-10-25", 25), ("2026-10-26", 24)])
def test_daily_baseline_uses_complete_local_calendar_day_and_independent_totals(
    archive, day, hours
):
    """A 1-kWh/h prediction versus 1.25-kWh/h actual gives 20 percent daily error over 23, 25 or 24 hours."""
    daily, record, now, _context = daily_fixture(archive, day)
    result = daily_evidence(
        record, archive, archive.documents, SqlDocuments(archive.documents.engine, "solar"), now
    )
    (source,) = result["series"]
    assert source["coverage"] == {"hours": hours, "measured_hours": hours, "paired_hours": hours}
    assert source["daily"] == {
        "predicted_kwh": hours,
        "measured_kwh": hours * 1.25,
        "error_kwh": -hours * 0.25,
        "absolute_error_kwh": hours * 0.25,
        "relative_error_percent": 20,
        "evaluation_issue": None,
    }
    assert DailyForecastArchive(archive.documents).read(day, "Europe/Amsterdam") == record
    assert daily.page(now, "Europe/Amsterdam")["days"][-1] == day


def test_daily_quality_waits_for_whole_day_and_recorder_delay(archive):
    """Even retained actuals cannot score a day before its final measurement becomes eligible after the ten-minute delay."""
    _daily, record, now, _context = daily_fixture(archive)
    result = daily_evidence(
        record,
        archive,
        archive.documents,
        SqlDocuments(archive.documents.engine, "solar"),
        now - timedelta(seconds=1),
    )
    (source,) = result["series"]
    assert source["daily"]["error_kwh"] is None
    assert "waits" in source["daily"]["evaluation_issue"]
    assert all(value is None for value in source["metrics"].values())
    assert all(row["error_kwh"] is None for row in source["rows"])


def test_daily_missing_actuals_do_not_become_zero_or_a_partial_day_score(archive):
    """Removing one cumulative endpoint leaves two unavailable hours and prevents a fabricated full-day total."""
    _daily, record, now, _context = daily_fixture(archive, missing=True)
    (source,) = daily_evidence(
        record, archive, archive.documents, SqlDocuments(archive.documents.engine, "solar"), now
    )["series"]
    assert source["daily"]["predicted_kwh"] == 24
    assert source["daily"]["measured_kwh"] is None
    assert source["daily"]["error_kwh"] is None
    assert all(value is None for value in source["metrics"].values())


def test_intraday_forecasts_and_plans_do_not_replace_midnight_baseline(archive):
    """New midday references and controls leave the frozen version unchanged. Controlled consumption still withholds accuracy."""
    daily, record, now, context = daily_fixture(archive)
    later = deepcopy(context)
    later["input_snapshot"]["demand_evidence"][0]["forecast_version"] = "later-forecast"
    daily.observe(
        datetime.fromisoformat(record["start"]) + timedelta(hours=12), record["timezone"], later
    )
    assert daily.read(record["day"], record["timezone"]) == record
    (source,) = daily_evidence(
        record,
        archive,
        archive.documents,
        SqlDocuments(archive.documents.engine, "solar"),
        now,
        ["sensor.house"],
    )["series"]
    assert source["forecast_version"] == "daily-original"
    assert source["daily"]["error_kwh"] is None
    assert "Controlled" in source["daily"]["evaluation_issue"]


def test_daily_controls_use_atomic_publication_time_and_survive_restart(
    archive, tmp_path, monkeypatch
):
    """A pre-midnight calculation published next day excludes only that day. Failed publication grants no controls."""
    from open_energy_planner.storage.adapters.lifecycle import sqlalchemy as repository_module
    from tests.database.contracts.test_plan_archive_backends import plan

    cutoff = datetime(2026, 10, 26, tzinfo=UTC)

    class PublicationClock(datetime):
        """Fix the real repository activation timestamp just after midnight."""

        instant = cutoff - timedelta(hours=1)

        @classmethod
        def now(cls, tz=None):
            """Return the publication instant in the requested zone."""
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(repository_module, "datetime", PublicationClock)
    repository = repository_module.SqlPlanRepository(tmp_path, engine=archive.documents.engine)
    PublicationClock.instant = cutoff + timedelta(seconds=2)
    document = plan()
    document["generated_at"] = (cutoff - timedelta(minutes=1)).isoformat()
    document["forecast_quality_controlled"] = ["sensor.house"]
    repository.state["plan"] = document
    write_plan = repository._write_plan

    def fail_write(*args):
        """Simulate failed publication before any activation can commit."""
        raise OSError("Unavailable archive")

    monkeypatch.setattr(repository, "_write_plan", fail_write)
    with pytest.raises(OSError):
        repository.save()
    assert controlled_consumers(repository, cutoff, cutoff + timedelta(days=1)) == []
    monkeypatch.setattr(repository, "_write_plan", write_plan)
    repository.state["plan"] = document
    repository.save()
    restored = repository_module.SqlPlanRepository(tmp_path, engine=archive.documents.engine)
    assert controlled_consumers(restored, cutoff - timedelta(days=1), cutoff) == []
    assert controlled_consumers(restored, cutoff, cutoff + timedelta(days=1)) == ["sensor.house"]
    assert controlled_consumers(
        restored, cutoff + timedelta(days=1), cutoff + timedelta(days=2)
    ) == ["sensor.house"]


@pytest.mark.parametrize("day", ["20261026", "2026-W44-1", "not-a-day"])
def test_daily_pagination_rejects_noncanonical_dates(archive, day):
    """Lexicographic day pagination rejects compact ISO and week dates as well as invalid text."""
    with pytest.raises(ValueError):
        DailyForecastArchive(archive.documents).page(START, "UTC", before=day)


def test_restart_after_midnight_does_not_invent_an_original_daily_prediction(archive):
    """Starting at noon leaves midnight unavailable instead of backdating today's current forecast."""
    daily = DailyForecastArchive(archive.documents)
    daily.observe(START, "Europe/Amsterdam", {"input_snapshot": {}})
    missing = daily.read("2026-10-26", "Europe/Amsterdam")
    assert missing["context"] is None and missing["captured_at"] is None
    assert not archive.documents.exists("quality-day-2026-10-26.json")


def test_missing_midnight_observation_cannot_use_a_later_forecast(archive):
    """A two-minute observation gap crosses midnight without sufficient capture evidence. Keep the day explicitly unavailable."""
    daily = DailyForecastArchive(archive.documents)
    midnight = START.replace(hour=0)
    context = {
        "forecast_quality_sources": [],
        "input_snapshot": {"demand_evidence": [], "solar_evidence": []},
    }
    daily.observe(midnight - timedelta(seconds=3), "UTC", context)
    daily.observe(midnight + timedelta(minutes=2), "UTC", context)
    assert daily.read("2026-10-26", "UTC")["context"] is None


def test_midnight_capture_rejects_a_forecast_with_a_future_issue_time(archive):
    """Even a cached reference cannot enter the midnight baseline if its declared issue time is after the cutoff."""
    daily = DailyForecastArchive(archive.documents)
    midnight = START.replace(hour=0)
    context = {
        "forecast_quality_sources": [],
        "input_snapshot": {
            "demand_evidence": [
                {
                    "forecast_version": "future",
                    "issued_at": (midnight + timedelta(seconds=1)).isoformat(),
                }
            ],
            "solar_evidence": [],
        },
    }
    daily.observe(midnight - timedelta(seconds=3), "UTC", context)
    daily.observe(midnight + timedelta(seconds=2), "UTC", context)
    assert daily.read("2026-10-26", "UTC")["context"]["input_snapshot"]["demand_evidence"] == []


def test_configured_source_without_midnight_forecast_remains_explicit(archive):
    """A missing roof forecast must remain visible as unavailable alongside a valid household comparison."""
    _daily, record, now, _context = daily_fixture(archive)
    record["context"]["expected_sources"] = [
        {"id": "household", "title": "Household consumption"},
        {"id": "roof", "title": "South roof"},
    ]
    result = daily_evidence(
        record, archive, archive.documents, SqlDocuments(archive.documents.engine, "solar"), now
    )
    assert [source["id"] for source in result["series"]] == ["household"]
    assert result["issues"] == ["Midnight forecast unavailable for South roof"]


def test_quality_daily_api_uses_frozen_day_without_a_successful_plan(tmp_path):
    """The authenticated day API reports 24 predicted versus 30 measured kWh from a retained baseline without needing any planning round."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from tests.ha_contract.support.metadata import setup_metadata

    repository = SqlPlanRepository(tmp_path)
    history = HistoryArchive(SqlDocuments(repository.engine, "demand"))
    _daily, record, _now, _context = daily_fixture(history, "2026-10-23")
    repository.close()

    async def scenario():
        """Use the real HTTP middleware, date validation and day evidence lookup."""
        token = "daily-quality-test-0123456789"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            path = "/v1/forecast-quality/days/2026-10-23"
            assert (await client.get(path)).status == 401
            assert (await client.get("/v1/forecast-quality/days")).status == 401
            headers = {"Authorization": "Bearer " + token}
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=setup_metadata())
            ).status == 200
            index = await (await client.get("/v1/forecast-quality/days", headers=headers)).json()
            assert "2026-10-23" in index["days"]
            assert set(index["current_models"]) == {"household", "solar", "consumers"}
            response = await client.get(path, headers=headers)
            assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            result = await response.json()
            assert result["version"] == record["version"]
            assert result["series"][0]["daily"]["predicted_kwh"] == 24
            assert result["series"][0]["daily"]["measured_kwh"] == 30
            assert result["series"][0]["daily"]["relative_error_percent"] == 20
            assert (
                await client.get("/v1/forecast-quality/days/2026-02-30", headers=headers)
            ).status == 400

    asyncio.run(scenario())


def test_daily_zero_actual_energy_has_no_relative_percentage_error(archive):
    """A complete measured zero day is valid evidence. The 24-kWh absolute error exists but division by zero does not."""
    _daily, record, now, _context = daily_fixture(archive)
    corrected = HistoryArchive(archive.documents)
    start = datetime.fromisoformat(record["start"])
    pending = corrected.request(now, [SOURCE])
    corrected.receive(
        pending,
        {
            "request_version": pending["version"],
            "entity_id": SOURCE["entity_id"],
            "unit": "kWh",
            "hours": [
                [(start + timedelta(hours=hour_index - 1)).timestamp(), 100, 100]
                for hour_index in range(25)
            ],
        },
        now,
    )
    (source,) = daily_evidence(
        record, corrected, archive.documents, SqlDocuments(archive.documents.engine, "solar"), now
    )["series"]
    assert source["daily"]["measured_kwh"] == 0
    assert source["daily"]["absolute_error_kwh"] == 24
    assert source["daily"]["relative_error_percent"] is None


def test_failed_midnight_write_retries_same_frozen_context(archive, monkeypatch):
    """A transient day-index write failure rolls back both documents. Retry retains the pre-midnight prediction after the capture window."""
    daily = DailyForecastArchive(archive.documents)
    cutoff = datetime(2026, 10, 26, tzinfo=UTC)
    context = {
        "forecast_quality_sources": [],
        "input_snapshot": {"demand_evidence": [], "solar_evidence": []},
    }
    daily.observe(cutoff - timedelta(seconds=3), "UTC", context)
    write = archive.documents.write

    def fail_index(name, value):
        """Fail after the baseline write to exercise transaction rollback and retry identity."""
        if name == "quality-days.json":
            raise OSError("Transient write failure")
        return write(name, value)

    monkeypatch.setattr(archive.documents, "write", fail_index)
    with pytest.raises(OSError):
        daily.observe(cutoff + timedelta(seconds=2), "UTC", context)
    assert not archive.documents.exists("quality-day-2026-10-26.json")
    original = deepcopy(daily.pending)
    monkeypatch.setattr(archive.documents, "write", write)
    daily.observe(cutoff + timedelta(minutes=2), "UTC", {"input_snapshot": {}})
    assert daily.read("2026-10-26", "UTC") == original


def test_original_quarter_predictions_hourly_actuals_gaps_and_independent_error_metrics(archive):
    """Actual hourly deltas are .5, missing and 2 kWh. Only two pairs score, giving MAE .75, bias -.25 and WAPE 60%."""
    receipt(archive, [(-1, 10), (0, 10.5), (2, 13), (3, 15)], now=START + timedelta(hours=5))
    actual, last_received = archive.read(SOURCE, START, START + timedelta(hours=4))
    assert actual == {START: 0.5, START + timedelta(hours=3): 2}
    forecast = prediction()
    forecast["steps"].extend(
        [
            {
                **row,
                "start": (datetime.fromisoformat(row["start"]) + timedelta(hours=3)).isoformat(),
            }
            for row in forecast["steps"][:4]
        ]
    )
    original = deepcopy(forecast)
    view = series(
        "house",
        "House",
        forecast,
        forecast["steps"],
        actual,
        START,
        START + timedelta(hours=4),
        START + timedelta(hours=5),
        receipt=last_received,
    )
    assert [row["measured_kwh"] for row in view["rows"]] == [0.5, None, None, 2]
    assert view["coverage"] == {"hours": 4, "measured_hours": 2, "paired_hours": 2}
    assert view["metrics"]["mae_kwh"] == 0.75
    assert view["metrics"]["bias_kwh"] == -0.25
    assert view["metrics"]["wape_percent"] == 60
    assert forecast == original


def test_hourly_archive_restart_corrections_and_removed_samples_preserve_old_blocks(archive):
    """A corrected cumulative endpoint changes latest actuals, retains the old block and never fills a deleted hour."""
    receipt(archive, [(-1, 10), (0, 11), (1, 13)])
    name = archive._pointer(SOURCE["version"], START.date().isoformat())
    old = archive.documents.read(name)
    restored = HistoryArchive(SqlDocuments(archive.documents.engine, "demand"))
    assert restored.read(SOURCE, START, START + timedelta(hours=2))[0] == {
        START: 1,
        START + timedelta(hours=1): 2,
    }
    receipt(restored, [(-1, 10), (0, 12)], now=START + timedelta(hours=5))
    assert restored.read(SOURCE, START, START + timedelta(hours=2))[0] == {START: 2}
    assert archive.documents.read(old["block"])["hours"][-1][1] == 13


def test_failed_source_does_not_starve_other_sources_and_stale_requests_do_not_write(archive):
    """A missing house meter advances to the next meter, while an obsolete receipt changes no retained data."""
    other = {**SOURCE, "entity_id": "sensor.pv", "version": "b" * 64}
    now = START + timedelta(hours=4)
    pending = archive.request(now, [SOURCE, other])
    with pytest.raises(ValueError, match="request changed"):
        archive.receive(
            pending, {"request_version": "obsolete", "error": "history_unavailable"}, now
        )
    assert archive.request(now, [SOURCE, other]) == pending
    archive.receive(
        pending, {"request_version": pending["version"], "error": "history_unavailable"}, now
    )
    assert archive.request(now, [SOURCE, other])["entity_id"] == "sensor.pv"


def test_replaced_ha_device_and_missing_channel_cannot_borrow_history(archive):
    """The same entity on a replacement physical device has a different source version and no inherited measurements."""
    receipt(archive, [(-1, 10), (0, 11)])
    replacement = {**SOURCE, "device_id": "other", "version": "b" * 64}
    assert archive.read(replacement, START, START + timedelta(hours=1)) == ({}, None)
    assert measured(
        archive,
        [SOURCE],
        [{"entity_id": "sensor.absent", "sign": 1}],
        START,
        START + timedelta(hours=1),
    ) == ({}, None)


def test_signed_household_balance_requires_every_meter_and_rejects_negative_reconstruction(archive):
    """1 kWh import minus .25 export gives .75 household demand. A missing export cannot imply zero export."""
    export = {**SOURCE, "entity_id": "sensor.export", "version": "b" * 64}
    receipt(archive, [(-1, 10), (0, 11)])
    receipt(archive, [(-1, 20), (0, 20.25)], source=export)
    terms = [{"entity_id": "sensor.house", "sign": 1}, {"entity_id": "sensor.export", "sign": -1}]
    actual, _received = measured(
        archive, [SOURCE, export], terms, START, START + timedelta(hours=1)
    )
    assert actual == {START: 0.75}
    assert measured(archive, [SOURCE], terms, START, START + timedelta(hours=1))[0] == {}


def frozen_plan(archive):
    """Retain a 1-kWh house, .4-kWh heat-pump and 1-kWh PV forecast, independently of later current models."""
    demand = archive.documents
    solar = SqlDocuments(demand.engine, "solar")
    forecast = prediction()
    forecast["steps"] = forecast["steps"][:4]
    forecast.update(configuration_version="house-definition", excluded_devices=[])
    demand.write("forecast-original.json", {"forecast": forecast})
    demand.write(
        "configuration-house-definition.json",
        {"terms": [{"entity_id": "sensor.house", "sign": 1}], "exclusions": []},
    )
    component = {
        **forecast,
        "version": "heat-original",
        "steps": [{**row, "kwh": 0.1} for row in forecast["steps"]],
    }
    demand.write(
        "consumer-forecast-composite-original.json",
        {
            "forecast": {**forecast, "version": "composite-original"},
            "components": {
                "devices": [
                    {
                        "entity_id": "sensor.heat",
                        "weather_sensitive": True,
                        "weather_features_used": True,
                        "forecast": component,
                    }
                ]
            },
        },
    )
    demand.write(
        "quality-components-original.json", {"version": "composite-original", "selected": False}
    )
    solar.write(
        "configuration-solar-definition.json",
        {
            "sources": [
                {
                    "source_id": "roof",
                    "entity_id": "sensor.pv",
                    "inverter_id": "roof-converter",
                    "measurement_boundary": "ac",
                    "can_curtail": False,
                }
            ],
            "inverters": [{"inverter_id": "roof-converter", "hybrid": False, "ac_limit_w": 5000}],
        },
    )
    solar.write(
        "forecast-pv-original.json",
        {
            "forecast": {
                "version": "pv-original",
                "issued_at": forecast["issued_at"],
                "model": "pvwatts-v2",
                "energies": [
                    {
                        "source_id": "roof",
                        "start": row["start"],
                        "duration_h": 0.25,
                        "ac_kwh": 0.25,
                        "dc_kwh": 0.3,
                    }
                    for row in forecast["steps"]
                ],
            }
        },
    )
    heat = {**SOURCE, "entity_id": "sensor.heat", "version": "b" * 64}
    pv = {**SOURCE, "entity_id": "sensor.pv", "version": "c" * 64}
    receipt(archive, [(-1, 10), (0, 12)])
    receipt(archive, [(-1, 10), (0, 10.5)], source=heat)
    receipt(archive, [(-1, 10), (0, 11)], source=pv)
    plan = {
        "plan_id": "original-plan",
        "generated_at": forecast["issued_at"],
        "forecast_quality_sources": [SOURCE, heat, pv],
        "forecast_quality_names": {"sensor.heat": "Heat pump"},
        "comparison_names": {"roof": "South roof"},
        "input_snapshot": {
            "problem": {
                "intervals": [
                    {"start": row["start"], "duration_h": 0.25} for row in forecast["steps"]
                ]
            },
            "demand_evidence": [
                {
                    "forecast_version": "original",
                    "configuration_version": "house-definition",
                    "model": "weekday-hour-v2",
                }
            ],
            "solar_evidence": [
                {
                    "forecast_version": "pv-original",
                    "configuration_version": "solar-definition",
                    "source_ids": ["roof"],
                }
            ],
        },
    }
    return plan, solar


@pytest.mark.parametrize("partial_components", [False, True])
def test_frozen_household_device_and_pv_evidence_survives_restart_without_hindsight(
    archive, partial_components
):
    """Original 1/.4/1-kWh predictions meet later 2/.5/1-kWh readings. Their independent MAEs are 1/.1/0 kWh."""
    plan, solar = frozen_plan(archive)
    if partial_components:
        components = archive.documents.read("consumer-forecast-composite-original.json")[
            "components"
        ]
        archive.documents.write(
            "quality-components-original.json", {"selected": False, "components": components}
        )
    old_plan = deepcopy(plan)
    result = evidence(
        plan,
        HistoryArchive(SqlDocuments(archive.documents.engine, "demand")),
        archive.documents,
        solar,
        START + timedelta(hours=4),
    )
    assert result["issues"] == []
    household, heat, pv = result["series"]
    assert [row["title"] for row in result["series"]] == [
        "Household consumption",
        "Heat pump",
        "South roof",
    ]
    assert household["metrics"]["mae_kwh"] == 1
    assert heat["metrics"]["mae_kwh"] == pytest.approx(0.1)
    assert pv["metrics"]["mae_kwh"] == 0
    assert heat["weather_features_used"] is True and heat["composition_selected"] is False
    assert [row["forecast_version"] for row in result["series"]] == [
        "original",
        "heat-original",
        "pv-original",
    ]
    assert plan == old_plan
    renamed = evidence(
        plan,
        archive,
        archive.documents,
        solar,
        START + timedelta(hours=4),
        names={"sensor.pv": "Garden extension", "sensor.heat": "Living room heat pump"},
    )
    assert [row["title"] for row in renamed["series"]] == [
        "Household consumption",
        "Living room heat pump",
        "Garden extension",
    ]
    for before, after in zip(result["series"], renamed["series"], strict=True):
        assert {key: value for key, value in before.items() if key != "title"} == {
            key: value for key, value in after.items() if key != "title"
        }
    assert plan == old_plan


@pytest.mark.parametrize(
    "scheduled_task", [False, True], ids=["baseline-exclusion", "appliance-task"]
)
def test_refinement_retains_original_household_and_component_archives(archive, scheduled_task):
    """Keep original 1/.7-kWh hourly forecasts through task/exclusion refinement. Six .3-kWh residual hours total 1.8. Controlled hours stay unscored."""
    from dataclasses import replace
    from types import SimpleNamespace

    from open_energy_planner.demand.basis import DemandEvidence
    from open_energy_planner.demand.calculation.forecasting import WeekdayProfile
    from open_energy_planner.demand.calculation.model import DemandForecast, ForecastStep
    from open_energy_planner.demand.storage import DemandStore
    from open_energy_planner.demand.wire import forecast_document
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot
    from tests.app.appliance.test_appliances import ENTITY, service, task

    tasks = service(archive.documents.engine)
    if scheduled_task:
        task(tasks, now=START)
    forecast = DemandForecast(
        "house-definition",
        "historical-fit",
        START - timedelta(minutes=5),
        "weekday-hour-v2",
        tuple(
            ForecastStep(START + timedelta(minutes=15 * interval_index), 0.25, 0.25)
            for interval_index in range(24)
        ),
        (),
    )
    component = replace(forecast, steps=tuple(replace(step, kwh=0.175) for step in forecast.steps))
    device = {
        "entity_id": ENTITY,
        "weather_sensitive": False,
        "weather_features_used": False,
        "forecast": forecast_document(component),
        "baseline_forecast": forecast_document(component),
    }
    residual = replace(forecast, steps=tuple(replace(step, kwh=0.075) for step in forecast.steps))
    tasks.consumers.result = {"devices": [device], "residual_baseline": forecast_document(residual)}
    store = DemandStore(archive.documents)
    store.archive(forecast, WeekdayProfile(8760, "UTC", ((0, 0, 1),), 1))
    archive.documents.write(
        "configuration-house-definition.json",
        {"terms": [{"entity_id": "sensor.house", "sign": 1}], "exclusions": []},
    )
    composite = replace(forecast, model="consumer-composite-v1")
    archive.documents.write(
        "consumer-forecast-" + composite.version + ".json",
        {"forecast": forecast_document(composite), "components": {"devices": [device]}},
    )
    store.archive_components(forecast, composite)
    original_evidence = DemandEvidence(
        forecast.version,
        forecast.configuration_version,
        forecast.training_version,
        forecast.model,
        forecast.quality,
        forecast.excluded_devices,
    )
    problem = InstallationProblem(
        tuple(
            Interval(START + timedelta(hours=interval_index), 1, 0.2, 0, 1)
            for interval_index in range(6)
        ),
        (),
        (),
        (),
        3000,
        3000,
    )
    snapshot = InputSnapshot(
        "snapshot",
        "configuration",
        START,
        "measurement",
        "forecast",
        "prices",
        problem,
        demand_evidence=(original_evidence,),
    )
    refined_snapshot = tasks.prepare(
        snapshot,
        SimpleNamespace(),
        START,
        excluded_entities=() if scheduled_task else (ENTITY,),
    )
    assert tasks.issue is None
    assert sum(step.load_kwh for step in refined_snapshot.problem.intervals) == pytest.approx(1.8)
    device_source = {**SOURCE, "entity_id": ENTITY, "version": "b" * 64}
    receipt(archive, [(-1, 10), (0, 12)])
    receipt(archive, [(-1, 10), (0, 10.5)], source=device_source)
    plan = {
        "plan_id": "refined-original",
        "generated_at": START.isoformat(),
        "input_snapshot": refined_snapshot.record(),
        "forecast_quality_sources": [SOURCE, device_source],
        "forecast_quality_names": {ENTITY: "Dishwasher"},
        "forecast_quality_controlled": [ENTITY],
    }
    result = evidence(
        plan,
        archive,
        archive.documents,
        SqlDocuments(archive.documents.engine, "solar"),
        START + timedelta(hours=7),
    )
    assert result["issues"] == []
    household, appliance = result["series"]
    assert refined_snapshot.demand_evidence == (original_evidence,)
    assert household["forecast_version"] == forecast.version
    assert household["rows"][0]["predicted_kwh"] == 1
    assert appliance["forecast_version"] == component.version
    assert appliance["rows"][0]["predicted_kwh"] == pytest.approx(0.7)
    assert household["rows"][0]["measured_kwh"] == 2
    assert appliance["rows"][0]["measured_kwh"] == 0.5
    assert household["metrics"]["mae_kwh"] is None
    assert appliance["metrics"]["mae_kwh"] is None


@pytest.mark.parametrize("boundary", ["hybrid", "shared", "curtailed", "clipped"])
def test_pv_physical_boundaries_withhold_accuracy_but_keep_measured_values(archive, boundary):
    """PV's 1-kWh meter reading stays visible, but unavailable production proof must not produce a perfect score."""
    plan, solar = frozen_plan(archive)
    definition = solar.read("configuration-solar-definition.json")
    if boundary == "hybrid":
        definition["inverters"][0]["hybrid"] = True
    elif boundary == "shared":
        definition["sources"].append({**definition["sources"][0], "source_id": "garage"})
    elif boundary == "curtailed":
        definition["sources"][0]["can_curtail"] = True
    else:
        definition["inverters"][0]["ac_limit_w"] = 1000
    solar.write("configuration-solar-definition.json", definition)
    result = evidence(plan, archive, archive.documents, solar, START + timedelta(hours=4))
    pv = next(row for row in result["series"] if row["id"] == "roof")
    assert pv["rows"][0]["measured_kwh"] == 1
    assert pv["coverage"]["paired_hours"] == 0
    assert pv["metrics"]["mae_kwh"] is None


def test_controlled_load_timing_never_claims_forecast_error_or_measured_savings(archive):
    """EMS-controlled heat-pump timing cannot score a historical behaviour forecast. Independent PV accuracy remains valid."""
    plan, solar = frozen_plan(archive)
    plan["forecast_quality_controlled"] = ["sensor.heat"]
    result = evidence(plan, archive, archive.documents, solar, START + timedelta(hours=4))
    assert [row["coverage"]["paired_hours"] for row in result["series"]] == [0, 0, 1]
    assert "savings" not in result


def test_failed_daily_pointer_rolls_back_new_block_and_keeps_retry_pending(archive, monkeypatch):
    """A pointer-write outage retains old 1-kWh actuals and does not acknowledge the replacement 2-kWh receipt."""
    receipt(archive, [(-1, 10), (0, 11)])
    now = START + timedelta(hours=5)
    pending = archive.request(now, [SOURCE])
    original_write = archive.documents.write

    def reject_pointer(name, value):
        """Fail after immutable inserts but before activating the source/day pointer."""
        if name.startswith("quality-history-"):
            raise OSError("fixture pointer outage")
        original_write(name, value)

    monkeypatch.setattr(archive.documents, "write", reject_pointer)
    with pytest.raises(OSError):
        archive.receive(
            pending,
            {
                "request_version": pending["version"],
                "entity_id": "sensor.house",
                "unit": "kWh",
                "hours": [
                    [(START - timedelta(hours=1)).timestamp(), 10, 10],
                    [START.timestamp(), 12, 12],
                ],
            },
            now,
        )
    assert archive.read(SOURCE, START, START + timedelta(hours=1))[0] == {START: 1}
    assert archive.request(now, [SOURCE]) == pending


def test_old_plans_without_frozen_source_bindings_are_explicitly_unavailable(archive):
    """Existing history is preserved. Newly configured sources must never be attached to an older plan retroactively."""
    result = evidence(
        {"plan_id": "old", "generated_at": START.isoformat()},
        archive,
        archive.documents,
        SqlDocuments(archive.documents.engine, "solar"),
        START,
    )
    assert result["series"] == []
    assert "predates" in result["issues"][0]


def test_quality_api_requires_authentication_and_preserves_original_plan(tmp_path):
    """Reading a legacy original plan reports unavailable evidence without rewriting it or allowing anonymous access."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from tests.frontend.browser.savings_fixture import seed

    seed(tmp_path)

    async def scenario():
        """Use actual app authorization and immutable archive reads, without activating a solver or a new model."""
        token = "forecast-quality-test-0123456789"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer " + token}
            path = "/v1/forecast-quality/browser-original-plan"
            assert (await client.get(path)).status == 401
            assert (await client.post("/v1/forecast-quality/history", json=[])).status == 401
            original = await (
                await client.get("/v1/plans/browser-original-plan", headers=headers)
            ).json()
            response = await client.get(path, headers=headers)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            result = await response.json()
            assert result["series"] == []
            assert result["issues"]
            assert result["current_models"]
            assert (await client.get("/v1/forecast-quality/missing", headers=headers)).status == 404
            assert (
                await client.post("/v1/forecast-quality/history", json=[], headers=headers)
            ).status == 400
            assert (
                await client.post("/v1/forecast-quality/history", json={}, headers=headers)
            ).status == 409
            assert (
                await (await client.get("/v1/plans/browser-original-plan", headers=headers)).json()
                == original
            )

    asyncio.run(scenario())


def test_original_component_reference_is_immutable_and_per_source_active_model_is_specific(
    tmp_path,
):
    """A household issue cannot acquire a later device forecast. A calibrated roof must not relabel a physical garage."""
    from types import SimpleNamespace

    from open_energy_planner.demand.storage import DemandStore
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore

    engine = create_engine("sqlite://")
    SCHEMA.create_all(engine)
    try:
        store = DemandStore(SqlDocuments(engine, "demand"))
        household = SimpleNamespace(version="household", model="weekday-hour-v2")
        composite = SimpleNamespace(version="same-issue")
        store.archive_components(household, composite)
        store.archive_components(household, composite)
        with pytest.raises(ValueError, match="reference changed"):
            store.archive_components(household, SimpleNamespace(version="later-issue"))
        assert store.documents.read("quality-components-household.json") == {
            "version": "same-issue",
            "selected": False,
        }
        runtime = SolarRuntime(SolarStore(SqlDocuments(engine, "solar")))
        runtime.baseline = SimpleNamespace(model="pvwatts-v1")
        runtime.source_selection = {"roof": {"model": "pvwatts-calibrated-v2"}}
        assert runtime.active_model("roof") == "pvwatts-calibrated-v2"
        assert runtime.active_model("garage") == "pvwatts-v1"
    finally:
        engine.dispose()


def test_midnight_correction_cannot_mix_old_and_new_cumulative_pointer_versions(
    archive, monkeypatch
):
    """A +100 kWh cumulative-baseline correction between block reads must keep all three hourly deltas at 1 kWh, never 101."""
    original = [(10, 10), (11, 11), (12, 12), (13, 13)]
    captured_at = START + timedelta(hours=15)
    receipt(archive, original, now=captured_at)
    read = archive.documents.read
    corrected = False

    def change_between_blocks(name):
        """Commit a real correction after the first old immutable block has been read."""
        nonlocal corrected
        value = read(name)
        if name.startswith("quality-block-") and not corrected:
            corrected = True
            receipt(
                archive,
                [(offset, total + 100) for offset, total in original],
                now=START + timedelta(hours=16),
            )
        return value

    monkeypatch.setattr(archive.documents, "read", change_between_blocks)
    values, received_at = archive.read(
        SOURCE, START + timedelta(hours=11), START + timedelta(hours=14)
    )
    assert corrected
    assert values == {START + timedelta(hours=offset): 1 for offset in (11, 12, 13)}
    assert received_at == captured_at.isoformat()
    assert (
        archive.read(SOURCE, START + timedelta(hours=11), START + timedelta(hours=14))[0] == values
    )


@pytest.mark.parametrize("has_evidence", [False, True])
def test_daily_activity_requires_accepted_original_forecast_evidence(archive, has_evidence):
    """A continuously observed midnight without forecast entries cannot claim availability or recovery."""
    import asyncio
    from copy import deepcopy

    from open_energy_planner.activities.events import Events, Outcome
    from open_energy_planner.storage.adapters.activities import ActivityStore

    _daily, record, _now, context = daily_fixture(archive)
    context = deepcopy(context)
    if not has_evidence:
        context["input_snapshot"]["demand_evidence"] = []
    start = datetime.fromisoformat(record["end"])

    async def scenario():
        """Archive the next real midnight on the shared backend with event-loop-owned outcomes."""
        daily = DailyForecastArchive(archive.documents)
        events = Events(ActivityStore(archive.documents.engine), lambda: start)
        events.loop = asyncio.get_running_loop()
        daily.events = events
        try:
            identity = await events.start("forecast-quality", "quality", "Earlier daily archive")
            await events.finish(identity, Outcome.FAILED, "Earlier missing evidence.")
            cursor = events.cursor
            await asyncio.to_thread(
                daily.observe, start - timedelta(seconds=3), "Europe/Amsterdam", context
            )
            await asyncio.to_thread(
                daily.observe, start + timedelta(seconds=2), "Europe/Amsterdam", context
            )
            await events.flush()
            terminal = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity" and row["value"]["state"] != "running"
            ]
            assert len(terminal) == 1
            assert terminal[0]["state"] == ("completed" if has_evidence else "failed")
            assert terminal[0]["recovered"] is has_evidence
            assert events.health["forecast-quality"]["state"] == (
                "available" if has_evidence else "unavailable"
            )
        finally:
            await events.close()

    asyncio.run(scenario())
