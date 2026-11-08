"""Discover multi-year solar availability with real HA executor ownership and bounded transfers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.custom_component.support.chain import running_chain


def test_solar_monthly_discovery_and_annual_capture_preserve_gaps_and_empty_years(monkeypatch):
    """Monthly range discovery returns metadata, while hourly chunks keep missing values absent and never infer an end."""
    from custom_components.open_energy_planner import history

    async def scenario():
        """Exercise the real Recorder executor with independently specified supported statistics responses."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            entity = "sensor.synthetic_solar"
            calls = []
            chunk_start = datetime(2024, 12, 31, 23, tzinfo=UTC)
            chunk_end = datetime(2026, 1, 1, tzinfo=UTC)

            def metadata(_hass, statistic_ids):
                """Expose the original MWh metadata identity while Recorder converts requested rows to kWh."""
                assert statistic_ids == {entity}
                return {
                    entity: (
                        77,
                        {"has_sum": True, "unit_of_measurement": "MWh", "source": "recorder"},
                    )
                }

            def statistics(_hass, start, end, statistic_ids, period, units, fields):
                """Return a coarse first month, a gap-containing year, or an explicitly empty year."""
                calls.append((start, end, period, units, fields))
                assert statistic_ids == {entity}
                if period == "month":
                    return {
                        entity: [
                            {"start": datetime(2023, 2, 1, tzinfo=UTC).timestamp(), "sum": 100}
                        ]
                    }
                assert period == "hour"
                if start != chunk_start:
                    return {entity: []}
                return {
                    entity: [
                        {"start": start.timestamp(), "sum": 100.0, "state": 100.0},
                        {
                            "start": (start + timedelta(hours=1)).timestamp(),
                            "sum": None,
                            "state": None,
                        },
                        {
                            "start": (start + timedelta(hours=2)).timestamp(),
                            "sum": 102.0,
                            "state": 102.0,
                        },
                    ]
                }

            monkeypatch.setattr(history, "get_metadata", metadata)
            monkeypatch.setattr(history, "statistics_during_period", statistics)
            bridge = history.HistoryBridge(hass)
            available = await bridge.capture(
                {
                    "version": "availability",
                    "basis": "solar_history_range",
                    "entity_id": entity,
                    "start": "1970-01-01T00:00:00+00:00",
                    "end": chunk_end.isoformat(),
                }
            )
            assert available["available_start"] == "2023-02-01T00:00:00+00:00"
            assert available["statistics_identity"] == {
                "metadata_id": 77,
                "source": "recorder",
                "unit": "MWh",
            }
            assert "hours" not in available
            annual = await bridge.capture(
                {
                    "version": "year",
                    "basis": "solar_history_chunk",
                    "entity_id": entity,
                    "start": chunk_start.isoformat(),
                    "end": chunk_end.isoformat(),
                }
            )
            assert annual["unit"] == "kWh"
            assert annual["statistics_identity"] == available["statistics_identity"]
            assert annual["hours"] == [
                [chunk_start.timestamp(), 100.0, 100.0],
                [(chunk_start + timedelta(hours=2)).timestamp(), 102.0, 102.0],
            ]
            empty = await bridge.capture(
                {
                    "version": "empty-year",
                    "basis": "solar_history_chunk",
                    "entity_id": entity,
                    "start": "2023-12-31T23:00:00+00:00",
                    "end": "2025-01-01T00:00:00+00:00",
                }
            )
            assert empty["hours"] == []
            assert empty["basis"] == "solar_history_chunk"
            assert [call[2] for call in calls] == ["month", "hour", "hour"]
            assert all(call[3] == {"energy": "kWh"} for call in calls)
            assert calls[0][4] == {"sum"}
            assert calls[1][4] == {"sum", "state"}

    asyncio.run(scenario())


@pytest.mark.parametrize("basis", [None, "statistics_sum", "solar_history_chunk"])
def test_solar_discovery_does_not_expand_hourly_transfer_ranges(monkeypatch, basis):
    """Only explicit monthly availability discovery may span decades. Hourly finance, demand and solar stay bounded."""
    from types import SimpleNamespace

    from custom_components.open_energy_planner import history

    async def scenario():
        """Reject the oversized request before any Recorder executor or metadata query can run."""
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        request = {
            "version": "oversized",
            "entity_id": "sensor.synthetic_solar",
            "start": "2023-01-01T00:00:00+00:00",
            "end": "2025-01-01T00:00:00+00:00",
        }
        if basis is not None:
            request["basis"] = basis
        with pytest.raises(ValueError, match="Invalid history request range"):
            await history.HistoryBridge(hass).capture(request)

    asyncio.run(scenario())
