"""Verify compact test imports preserve hourly evidence and leave full laboratory imports available."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.auth import auth_manager_from_config
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import statistics_during_period

from test_environment.oep_test_fixture import dishwasher, runtime
from tests.custom_component.support import fixture_history
from tests.custom_component.support.chain import running_chain, setup_energy


def test_compact_history_preserves_hourly_origin_and_restores_full_imports(monkeypatch):
    """Keep all 96 hourly records while compact fine data and full imports alternate.

    Four days at one kWh/hour increase state 100 to 196. The native zero-ended
    sum origin makes the first hourly state 101 correspond to sum -95.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    monkeypatch.setattr(fixture_history, "utcnow", lambda: now)

    async def scenario():
        """Read genuine Recorder output through native APIs across compact, full and compact-again chains."""

        async def read_history(source_hass, history_start, history_end, period):
            """Read one stored interval without bypassing native Recorder semantics."""
            return await get_instance(source_hass).async_add_executor_job(
                partial(
                    statistics_during_period,
                    source_hass,
                    history_start,
                    history_end,
                    {"sensor.fixture_window"},
                    period,
                    {"energy": "kWh"},
                    {"sum", "state"},
                )
            )

        for age_days, compact_history in ((0, True), (0, False), (0, True), (5, True), (5, False)):
            history_end = now - timedelta(days=age_days)
            history_start = history_end - timedelta(days=4)
            fine_statistics = [
                {
                    "start": history_start + timedelta(minutes=5 * interval_index),
                    "state": 100 + (interval_index + 1) / 12,
                    "sum": 100 + (interval_index + 1) / 12,
                }
                for interval_index in range(4 * 24 * 12)
            ]
            hourly_statistics = [
                {
                    "start": history_start + timedelta(hours=hour_index),
                    "state": 101 + hour_index,
                    "sum": 101 + hour_index,
                }
                for hour_index in range(4 * 24)
            ]
            original_statistics = deepcopy((fine_statistics, hourly_statistics))
            async with running_chain(recorder=True, compact_history=compact_history) as (hass, *_):
                metadata = {
                    "statistic_id": "sensor.fixture_window",
                    "source": "recorder",
                    "name": "Fixture window",
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                }
                await runtime.import_fine(hass, metadata, fine_statistics, hourly_statistics)
                recorder = get_instance(hass)
                await recorder.async_block_till_done()

                hourly_rows = (await read_history(hass, history_start, history_end, "hour"))[
                    "sensor.fixture_window"
                ]
                assert len(hourly_rows) == 96
                assert hourly_rows[0]["state"] == 101
                assert hourly_rows[0]["sum"] == -95
                assert hourly_rows[-1]["state"] == 196 and hourly_rows[-1]["sum"] == 0
                old_fine_rows = (
                    await read_history(
                        hass, history_start, history_start + timedelta(hours=1), "5minute"
                    )
                ).get("sensor.fixture_window", [])
                assert len(old_fine_rows) == (0 if compact_history else 12)
                recent_fine_rows = (
                    await read_history(
                        hass, history_end - timedelta(hours=1), history_end, "5minute"
                    )
                )["sensor.fixture_window"]
                assert len(recent_fine_rows) == (1 if age_days and compact_history else 12)
                two_day_fine_rows = (
                    await read_history(
                        hass, history_end - timedelta(days=2), history_end, "5minute"
                    )
                )["sensor.fixture_window"]
                assert len(two_day_fine_rows) == (1 if age_days and compact_history else 576)
                assert recent_fine_rows[-1]["state"] == 196 and recent_fine_rows[-1]["sum"] == 0
                assert (fine_statistics, hourly_statistics) == original_statistics

    asyncio.run(scenario())


def test_appliance_generator_keeps_hourly_history_and_compacts_fine_imports(monkeypatch):
    """A real four-day generator retains 96 hours and only the recent 576 five-minute points."""
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = now - timedelta(days=4)
    monkeypatch.setattr(fixture_history, "utcnow", lambda: now)
    monkeypatch.setattr(dishwasher, "dt_util", SimpleNamespace(utcnow=lambda: now))
    monkeypatch.setattr(dishwasher, "HISTORY_DAYS", 4)

    async def scenario():
        """Read native Recorder output after the actual appliance generator imports its own data."""
        async with running_chain(recorder=True) as (hass, entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            await dishwasher.setup_dishwasher(hass, entry)
            recorder = get_instance(hass)
            await recorder.async_block_till_done()
            entity_id = hass.data[dishwasher.DATA_KEY]["ids"]["energy"]
            for period, expected_count, expected_start in (
                ("hour", 96, history_start),
                ("5minute", 576, now - timedelta(days=2)),
            ):
                result = await recorder.async_add_executor_job(
                    partial(
                        statistics_during_period,
                        hass,
                        history_start,
                        now,
                        {entity_id},
                        period,
                        {"energy": "kWh"},
                        {"sum", "state"},
                    )
                )
                stored_rows = result[entity_id]
                assert len(stored_rows) == expected_count
                assert stored_rows[0]["start"] == expected_start.timestamp()
                assert stored_rows[-1]["sum"] == 0

    asyncio.run(scenario())
