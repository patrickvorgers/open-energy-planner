"""Portable day closure preserves gaps, independent scenario coverage and immutable checkpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from open_energy_planner.core.calendar import day_window
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings, finalization_time
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATIONS,
    PUBLISHED_PRICES,
    SAVINGS_DAYS,
)
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame


def test_daily_partial_results_keep_measured_totals_without_extrapolating(settlement):
    """Two measured quarters cost .60 actual and .40 reference, rather than pretending to cover a whole day."""
    runtime, _store, _docs, _engine, _plans = settlement
    daily = DailySavings(runtime)
    report = daily.calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    assert report["expected_seconds"] == 86400
    assert report["scenarios"]["actual"]["coverage_seconds"] == 1800
    assert report["scenarios"]["actual"]["status"] == "partial"
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.6)
    assert report["scenarios"]["reference"]["totals"]["economic_cost"] == pytest.approx(0.4)
    assert report["scenarios"]["without_battery"]["totals"]["economic_cost"] == pytest.approx(0.6)
    assert report["savings"] == pytest.approx(-0.2)
    assert report["comparison_seconds"] == 1800
    assert report["checkpoint"]["reference_known"] is False
    assert report["scenarios"]["actual"]["missing"][-1]["end"] == "2026-10-23T00:00:00+00:00"


def test_later_actual_and_without_battery_do_not_reset_unknown_reference(settlement):
    """A missing opening quarter leaves reference unavailable while two valid actual intervals still cost .60."""
    runtime, _store, _docs, _engine, _plans = settlement
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        contract(),
        None,
        END + timedelta(days=1),
        START - timedelta(minutes=15),
        None,
    )
    assert report["scenarios"]["reference"]["status"] == "unavailable"
    assert report["scenarios"]["reference"]["totals"] is None
    assert report["savings"] is None
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.6)
    assert report["scenarios"]["without_battery"]["totals"]["purchase"] == pytest.approx(0.6)
    assert report["checkpoint"]["reference_known"] is False


def test_final_day_survives_restart_and_rejects_corrections(settlement):
    """A sealed partial day retains its quality and exact financial evidence after a restart."""
    runtime, store, docs, engine, _plans = settlement
    daily = DailySavings(runtime)
    now = datetime(2026, 10, 26, tzinfo=UTC)
    docs.write("daily-upgrade.json", {"day": "2026-10-22", "timezone": "UTC"})
    report = daily.process(contract(), None, now, SiteLocation(52, 6, "UTC"))
    assert report["finalized_at"] == now.isoformat()
    assert store.day(finalized=True) == report
    assert report["scenarios"]["actual"]["status"] == "partial"
    with engine.connect() as connection:
        assert connection.scalar(select(SAVINGS_DAYS.c.finalized_at)) == now.isoformat()
    changed = {**report, "savings": 123}
    with pytest.raises(ValueError, match="Final daily savings are immutable"):
        store.put_day(changed, docs, "2026-10-23")
    assert store.day(finalized=True) == report
    assert docs.read("daily-progress.json")["next"] == "2026-10-23"


def test_provisional_day_accepts_late_evidence_before_calendar_deadline(settlement):
    """Provisional reports remain replaceable until three complete recovery days have elapsed."""
    runtime, store, docs, _engine, _plans = settlement
    daily = DailySavings(runtime)
    first = daily.process(
        contract(), None, datetime(2026, 10, 23, tzinfo=UTC), SiteLocation(52, 6, "UTC")
    )
    assert first["finalized_at"] is None
    second = {**first, "evaluated_at": "2026-10-24T00:00:00+00:00"}
    store.put_day(second, docs, "2026-10-23")
    assert store.day(finalized=False) == second


@pytest.mark.parametrize(
    "day, expected",
    [
        ("2026-03-27", "2026-03-30T22:00:00+00:00"),
        ("2026-10-23", "2026-10-26T23:00:00+00:00"),
    ],
)
def test_finalization_respects_three_complete_local_days_across_dst(day, expected):
    """The local midnight deadline follows three recovery dates, rather than adding 72 absolute hours."""
    assert finalization_time(day, "Europe/Amsterdam").isoformat() == expected


@pytest.mark.parametrize(
    "day, timezone, quarters",
    [
        ("2026-10-22", "UTC", 96),
        ("2026-10-25", "Europe/Amsterdam", 100),
    ],
)
def test_complete_days_use_every_quarter_and_independent_cost_oracles(
    settlement, day, timezone, quarters
):
    """Actual imports cost .30 per quarter. Two reference battery kWh avoid .60 but consume .40 inventory value."""
    runtime, store, _docs, engine, _plans = settlement
    start, end = day_window(day, timezone)
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(start), plan_id="original-plan")
        )
        retain(
            connection,
            (
                PriceSeries(
                    "day-published",
                    "NL",
                    "EUR",
                    start - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            start + timedelta(minutes=index * 15),
                            start + timedelta(minutes=(index + 1) * 15),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(quarters)
                    ),
                ),
            ),
        )
    context = {"bindings": bindings(), "seeds": [], "location": None}
    store.put_frames(
        tuple(frame(start=start + timedelta(minutes=index * 15)) for index in range(quarters)),
        context,
    )
    daily = DailySavings(runtime)
    report = daily.calculate(day, timezone, contract(), None, end + timedelta(hours=1), start, None)
    assert report["expected_seconds"] == quarters * 900
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(quarters * 0.3)
    assert report["scenarios"]["reference"]["totals"]["purchase"] == pytest.approx(
        (quarters - 2) * 0.3
    )
    assert report["scenarios"]["reference"]["totals"]["inventory_change"] == pytest.approx(-0.4)
    assert report["scenarios"]["without_battery"]["totals"]["purchase"] == pytest.approx(
        quarters * 0.3
    )
    assert report["savings"] == pytest.approx(-0.2)
    assert all(scenario["status"] == "complete" for scenario in report["scenarios"].values())
    assert report["checkpoint"]["reference"]["reference_stored"] == {"battery": 0}


def test_upgrade_does_not_immediately_seal_old_missing_data(settlement):
    """Old partial history gets a recovery window after upgrade rather than losing its first pending HA response."""
    runtime, store, _docs, _engine, _plans = settlement
    report = DailySavings(runtime).process(
        contract(), None, datetime(2026, 11, 1, 12, tzinfo=UTC), SiteLocation(52, 6, "UTC")
    )
    assert report["finalized_at"] is None
    assert report["finalizes_after"] == "2026-11-05T00:00:00+00:00"
    assert store.day(finalized=True) is None


def test_missing_price_does_not_erase_physical_reference_progress(settlement):
    """The unpriced first quarter still consumes one reference kWh, so the priced second quarter starts with one."""
    runtime, _store, _docs, engine, _plans = settlement
    with engine.begin() as connection:
        connection.execute(
            PUBLISHED_PRICES.delete().where(PUBLISHED_PRICES.c.start == stamp(START))
        )
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    reference = report["scenarios"]["reference"]
    assert reference["coverage_seconds"] == 900
    assert reference["opening"]["stored_kwh"] == {"battery": 1}
    assert reference["closing"]["stored_kwh"] == {"battery": 0}
    assert reference["totals"]["economic_cost"] == pytest.approx(0.2)
    assert report["savings"] == pytest.approx(-0.1)
    assert report["energy"]["grid_import"] == {"kwh": 2, "coverage_seconds": 1800}


def test_unpriced_export_consumes_each_scenarios_own_bonus_allowance(settlement):
    """Unpriced .50-kWh export exhausts actual/no-battery quota while self-consumption stores PV. Next priced benefit is -.15."""
    import json
    from dataclasses import asdict, replace

    from open_energy_planner.contracts.bonus import CounterSeed
    from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_FRAMES
    from tests.app.savings.support.scenarios import problem

    runtime, store, _docs, engine, plans = settlement
    original = plans.get_plan("original-plan")
    solar_problem = problem(pv_kwh=1, load_kwh=0)
    solar_problem = replace(
        solar_problem, intervals=(replace(solar_problem.intervals[0], duration_h=0.5),)
    )
    plans.state["plan"] = {
        **original,
        "plan_id": "bonus-solar-context",
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(solar_problem), default=lambda value: value.isoformat())
            )
        },
    }
    plans.save()
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.update()
            .where(ACTIVATIONS.c.activated_at == stamp(START))
            .values(plan_id="bonus-solar-context")
        )
        connection.execute(
            PUBLISHED_PRICES.delete().where(PUBLISHED_PRICES.c.start == stamp(START))
        )
        connection.execute(SETTLEMENT_FRAMES.delete())
    seed = CounterSeed("bonus", START - timedelta(days=1), 1.5, 10, "sensor.export", START)
    seed_wire = json.loads(json.dumps(asdict(seed), default=lambda value: value.isoformat()))
    measured = frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5)
    later = replace(
        frame(start=END, imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5),
        meter_start={"sensor.export": 10.5},
    )
    store.put_frames(
        (measured, later), {"bindings": bindings(), "seeds": [seed_wire], "location": None}
    )
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(bonus=True), None, END + timedelta(days=1), START, None
    )
    assert report["scenarios"]["reference"]["coverage_seconds"] == 900
    assert report["scenarios"]["reference"]["opening"]["stored_kwh"] == {"battery": 2.5}
    assert report["scenarios"]["reference"]["totals"]["economic_cost"] == pytest.approx(-0.1)
    assert report["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(0.05)
    assert report["scenarios"]["actual"]["totals"]["bonus"] == 0
    assert report["scenarios"]["without_battery"]["totals"]["bonus"] == 0
    assert report["scenarios"]["without_battery"]["coverage_seconds"] == 900
    assert report["savings"] == pytest.approx(-0.15)


def test_partial_price_day_continues_after_sealing_and_expiring_raw_quarters(settlement):
    """After one unpriced quarter, 95 priced quarters cost 28.50 actual and 28.40 reference. Next day both cost .30."""
    import json
    from dataclasses import asdict

    from open_energy_planner.contracts.bonus import CounterSeed
    from open_energy_planner.storage.adapters.lifecycle.retention import thin

    runtime, store, docs, engine, plans = settlement
    start, end = day_window("2026-10-22", "UTC")
    seed = CounterSeed("bonus", START - timedelta(days=1), 1.5, 10, "sensor.export", start)
    context = {
        "bindings": bindings(),
        "seeds": [json.loads(json.dumps(asdict(seed), default=lambda value: value.isoformat()))],
        "location": None,
    }
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(start), plan_id="original-plan")
        )
        retain(
            connection,
            (
                PriceSeries(
                    "complete-day",
                    "NL",
                    "EUR",
                    start - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            start + timedelta(minutes=index * 15),
                            start + timedelta(minutes=(index + 1) * 15),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(97)
                    ),
                ),
            ),
        )
        connection.execute(
            PUBLISHED_PRICES.delete().where(PUBLISHED_PRICES.c.start == stamp(start))
        )
        # Replace the fixture's two context-free frames before adding the complete seeded day.
        from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_FRAMES

        connection.execute(SETTLEMENT_FRAMES.delete())
    store.put_frames(
        tuple(frame(start=start + timedelta(minutes=index * 15)) for index in range(96)), context
    )
    daily = DailySavings(runtime)
    report = daily.calculate(
        "2026-10-22", "UTC", contract(bonus=True), None, end + timedelta(days=4), start, None
    )
    assert report["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(28.5)
    assert report["scenarios"]["reference"]["totals"]["economic_cost"] == pytest.approx(28.4)
    assert report["checkpoint"]["physical_known"] is True
    assert report["checkpoint"]["reference_known"] is True
    assert list(report["checkpoint"]["reference_counters"].values()) == [1.5]
    report["finalized_at"] = (end + timedelta(days=4)).isoformat()
    store.put_day(report, docs, "2026-10-23")
    thin(plans, docs, end + timedelta(days=4), "UTC")
    assert store.frames() == []
    store.put_frames((frame(start=end),), context)
    following = daily.calculate(
        "2026-10-23",
        "UTC",
        contract(bonus=True),
        None,
        end + timedelta(days=4),
        start,
        store.day(finalized=True)["checkpoint"],
    )
    assert following["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(0.3)
    assert following["scenarios"]["reference"]["totals"]["economic_cost"] == pytest.approx(0.3)
    assert following["scenarios"]["reference"]["opening"]["stored_kwh"] == {"battery": 0}
    assert following["savings"] == pytest.approx(0)


def test_daily_read_cannot_mix_concurrently_corrected_details_and_totals(settlement, monkeypatch):
    """A .60/- .20 snapshot remains consistent while another connection publishes a .80/- .40 correction."""
    runtime, store, docs, _engine, _plans = settlement
    daily = DailySavings(runtime)
    old = daily.calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    corrected = daily.calculate(
        "2026-10-22", "UTC", contract(buy=".4"), None, END + timedelta(days=2), START, None
    )
    store.put_day(old, docs, "2026-10-23")
    summary = store.daily_summary

    def publish_after_totals(*arguments, **keywords):
        """Correct the real database after the first SELECT has established the read snapshot."""
        result = summary(*arguments, **keywords)
        store.put_day(corrected, docs, "2026-10-23")
        return result

    monkeypatch.setattr(store, "daily_summary", publish_after_totals)
    result = daily.overview(START.replace(hour=0), START.replace(hour=0) + timedelta(days=1))
    assert result["totals"]["actual"]["purchase"] == pytest.approx(0.6)
    assert result["days"][0]["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.6)
    assert result["chart"][0]["savings"] == pytest.approx(-0.2)
    assert store.day()["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.8)


def test_daily_http_projection_returns_three_scenarios_without_internal_checkpoint(settlement):
    """The real route serves sealed daily costs and coverage instead of replaying or exposing internal continuation."""
    import asyncio
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.savings.api import register

    runtime, store, docs, _engine, plans = settlement
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=4), START, None
    )
    report["finalized_at"] = (END + timedelta(days=4)).isoformat()
    report["device_names"] = {"battery": "Archived battery"}
    store.put_day(report, docs, "2026-10-23")

    async def request_report():
        """Keep the actual financial worker and HTTP response projection on the same isolated database."""
        app = web.Application()
        catalogue = SimpleNamespace(
            value={
                "entities": [{"entity_id": "sensor.discharge", "energy_name": "Current battery"}],
                "suggestions": {"batteries": [{"energy_entity": "sensor.discharge"}]},
            }
        )
        register(
            app,
            plans,
            SimpleNamespace(current=contract()),
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(configuration=None),
            SimpleNamespace(configuration=None),
            asyncio.Lock(),
            catalogue,
            lambda: SiteLocation(52, 6, "UTC"),
            mutation_lock=asyncio.Lock(),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                "/v1/savings",
                params={
                    "start": START.replace(hour=0).isoformat(),
                    "end": (START.replace(hour=0) + timedelta(days=1)).isoformat(),
                },
            )
            assert response.status == 200
            data = await response.json()
            assert data["daily"] is True
            assert set(data["totals"]) == {"actual", "reference", "without_battery"}
            assert data["totals"]["actual"]["purchase"] == pytest.approx(0.6)
            assert data["savings"] == pytest.approx(-0.2)
            assert data["days"][0]["finalized_at"] is not None
            assert "checkpoint" not in data["days"][0]
            assert response.headers["Cache-Control"] == "no-store"
            assert data["days"][0]["device_names"]["battery"] == "Current battery"
            catalogue.value["entities"][0]["energy_name"] = "Renamed battery"
            renamed = await client.get(
                "/v1/savings",
                params={
                    "start": START.replace(hour=0).isoformat(),
                    "end": (START.replace(hour=0) + timedelta(days=1)).isoformat(),
                },
            )
            renamed_data = await renamed.json()
            assert renamed_data["days"][0]["device_names"]["battery"] == "Renamed battery"
            assert renamed_data["totals"] == data["totals"]
            catalogue.value["suggestions"]["batteries"] = []
            removed = await client.get(
                "/v1/savings",
                params={
                    "start": START.replace(hour=0).isoformat(),
                    "end": (START.replace(hour=0) + timedelta(days=1)).isoformat(),
                },
            )
            assert (await removed.json())["days"][0]["device_names"][
                "battery"
            ] == "Archived battery"
            assert store.day(finalized=True)["device_names"] == {"battery": "Archived battery"}

    asyncio.run(request_report())
