"""Retain a synthetic full-day plan and midnight baseline with independently known partial PV accuracy."""

from datetime import UTC, datetime, timedelta
from math import fsum

from open_energy_planner.forecast_quality.daily import DailyForecastArchive
from open_energy_planner.forecast_quality.history import HistoryArchive
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarInverter,
    SolarSource,
)
from open_energy_planner.solar.curtailment import CurtailmentHistory, save_feedback
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import configuration_document
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository


def seed(path):
    """Original/alternative errors are .07/.035 kWh over 17 eligible hours. The incomplete comparable day stays unscored."""
    start = datetime(2026, 6, 15, tzinfo=UTC)
    end = start + timedelta(days=1)
    actual = (0,) * 6 + (0.1, 0.25, 0.5, 0.8, 1.1, 1.4, 1.5, 1.4, 1.1, 0.8, 0.5, 0.2) + (0,) * 6
    assert len(actual) == 24
    source = {
        "entity_id": "sensor.synthetic_pv",
        "device_id": "synthetic-meter",
        "unit": "kWh",
        "version": "c" * 64,
    }
    with_repository = SqlPlanRepository(path)
    try:
        documents = SqlDocuments(with_repository.engine, "demand")
        solar = SolarStore(SqlDocuments(with_repository.engine, "solar"))
        configuration = SolarConfiguration(
            (
                SolarSource(
                    "roof",
                    source["entity_id"],
                    "inverter",
                    5000,
                    "south",
                    curtailment_entity_id="binary_sensor.synthetic_curtailment",
                ),
            ),
            (SolarInverter("inverter", 5000),),
        )
        # This is an archived source, without a current installation or weather dependency.
        solar.archive("configuration", configuration.version, configuration_document(configuration))
        original = {
            "version": "synthetic-solar-original",
            "configuration_version": configuration.version,
            "issued_at": (start - timedelta(minutes=5)).isoformat(),
            "model": "pvwatts-v2",
            "energies": [
                {
                    "source_id": "roof",
                    "start": (start + timedelta(hours=index)).isoformat(),
                    "duration_h": 1,
                    "ac_kwh": value * 1.2,
                    "dc_kwh": value * 1.25,
                }
                for index, value in enumerate(actual)
            ],
        }
        alternative = {
            **original,
            "version": "synthetic-solar-alternative",
            "model": "solar-xgboost-production-v2",
            "energies": [
                {**row, "ac_kwh": actual[index] * 1.1}
                for index, row in enumerate(original["energies"])
            ],
        }
        solar.archive("forecast", original["version"], {"forecast": original})
        solar.archive("forecast", alternative["version"], {"forecast": alternative})
        solar.archive(
            "quality-candidates", original["version"], {"forecasts": [alternative["version"]]}
        )
        plan = {
            "plan_id": "synthetic-feedback-plan",
            "generated_at": original["issued_at"],
            "forecast_quality_sources": [source],
            "forecast_quality_names": {},
            "comparison_names": {"roof": "Synthetic south roof"},
            "input_snapshot": {
                "problem": {
                    "intervals": [
                        {"start": row["start"], "duration_h": 1} for row in original["energies"]
                    ]
                },
                "demand_evidence": [],
                "solar_evidence": [
                    {
                        "forecast_version": original["version"],
                        "configuration_version": configuration.version,
                        "source_ids": ["roof"],
                    }
                ],
            },
        }
        with_repository.state["plan"] = plan
        with_repository.save()
        daily = DailyForecastArchive(documents)
        daily_context = {
            **plan,
            "input_snapshot": {
                "demand_evidence": [],
                "solar_evidence": [
                    {
                        **plan["input_snapshot"]["solar_evidence"][0],
                        "issued_at": original["issued_at"],
                    }
                ],
            },
        }
        daily.observe(start - timedelta(seconds=3), "UTC", daily_context)
        daily.observe(start + timedelta(seconds=2), "UTC", daily_context)
        archive = HistoryArchive(documents)
        now = end + timedelta(minutes=15)
        pending = archive.request(now, [source])
        assert pending is not None
        archive.receive(
            pending,
            {
                "request_version": pending["version"],
                "entity_id": source["entity_id"],
                "unit": "kWh",
                "hours": [
                    [
                        (start + timedelta(hours=index - 1)).timestamp(),
                        100 + fsum(actual[:index]),
                        100 + fsum(actual[:index]),
                    ]
                    for index in range(25)
                ],
            },
            now,
        )
        save_feedback(
            solar,
            None,
            CurtailmentHistory(
                "binary_sensor.synthetic_curtailment",
                start + timedelta(hours=22),
                (
                    (start, False),
                    (start + timedelta(hours=8, minutes=30), True),
                    (start + timedelta(hours=10, minutes=30), False),
                    (start + timedelta(hours=15, minutes=30), None),
                    (start + timedelta(hours=16, minutes=30), False),
                ),
            ),
            start - timedelta(hours=1),
        )
    finally:
        with_repository.close()
