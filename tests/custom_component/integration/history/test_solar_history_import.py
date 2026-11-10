"""Discover multi-year solar availability with real HA executor ownership and bounded transfers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.custom_component.support.chain import running_chain


@pytest.mark.parametrize(
    "basis",
    [
        None,
        "energy_history_range",
        "energy_history_chunk",
        "solar_history_range",
        "solar_history_chunk",
    ],
)
def test_recorder_capture_failure_reports_request_effect_and_preserves_retry(
    monkeypatch, caplog, basis
):
    """Actual Recorder executor failures describe energy loss or bonus fallback without changing failure and retry contracts."""
    from custom_components.open_energy_planner import history

    async def scenario():
        """Fail a real capture's metadata read and verify same-version backoff and a new-version retry."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            attempts = []

            def fail_metadata(_hass, statistic_ids):
                """Interrupt the Recorder read after executor admission rather than faking its response."""
                attempts.append(statistic_ids)
                raise RuntimeError("Synthetic Recorder read failure")

            monkeypatch.setattr(history, "get_metadata", fail_metadata)
            bridge = history.HistoryBridge(hass)
            request = {
                "version": "failed-history",
                "entity_id": "sensor.synthetic_energy",
                "start": "1970-01-01T00:00:00+00:00"
                if basis and basis.endswith("_range")
                else "2026-01-01T00:00:00+00:00",
                "end": "2026-01-02T00:00:00+00:00",
            }
            if basis is not None:
                request["basis"] = basis
            caplog.clear()
            assert await bridge.capture(request) == {
                "request_version": "failed-history",
                "error": "history_unavailable",
            }
            warnings = [
                record.getMessage() for record in caplog.records if record.name == history.__name__
            ]
            assert warnings == [
                "Recorder energy history read failed. No history was returned for this request."
                if basis is not None
                else "Recorder history read failed. Manual bonus baseline remains available"
            ]
            retry_after = bridge.retry_after
            assert retry_after is not None
            assert await bridge.capture(request) is None
            assert bridge.retry_after == retry_after
            assert len(attempts) == 1
            assert await bridge.capture({**request, "version": "new-history"}) == {
                "request_version": "new-history",
                "error": "history_unavailable",
            }
            assert len(attempts) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("prefix", ["solar", "energy"])
def test_solar_monthly_discovery_and_annual_capture_preserve_gaps_and_empty_years(
    monkeypatch, prefix
):
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
                if start != chunk_start - timedelta(hours=1):
                    return {entity: []}
                return {
                    entity: [
                        {"start": chunk_start.timestamp() - 16, "sum": 100.0, "state": 100.0},
                        {
                            "start": (chunk_start + timedelta(hours=1)).timestamp(),
                            "sum": None,
                            "state": None,
                        },
                        {
                            "start": (chunk_start + timedelta(hours=2)).timestamp() + 64,
                            "sum": 102.0,
                            "state": 0.0,
                        },
                    ]
                }

            monkeypatch.setattr(history, "get_metadata", metadata)
            monkeypatch.setattr(history, "statistics_during_period", statistics)
            bridge = history.HistoryBridge(hass)
            available = await bridge.capture(
                {
                    "version": "availability",
                    "basis": prefix + "_history_range",
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
                    "basis": prefix + "_history_chunk",
                    "entity_id": entity,
                    "start": chunk_start.isoformat(),
                    "end": chunk_end.isoformat(),
                }
            )
            assert annual["unit"] == "kWh"
            assert annual["statistics_identity"] == available["statistics_identity"]
            assert annual["hours"] == [
                [chunk_start.timestamp() - 16, 100.0, 100.0],
                [(chunk_start + timedelta(hours=1)).timestamp(), None, None],
                [(chunk_start + timedelta(hours=2)).timestamp() + 64, 102.0, 0.0],
            ]
            empty = await bridge.capture(
                {
                    "version": "empty-year",
                    "basis": prefix + "_history_chunk",
                    "entity_id": entity,
                    "start": "2023-12-31T23:00:00+00:00",
                    "end": "2025-01-01T00:00:00+00:00",
                }
            )
            assert empty["hours"] == []
            assert empty["basis"] == prefix + "_history_chunk"
            assert [call[2] for call in calls] == ["month", "hour", "hour"]
            assert all(call[3] == {"energy": "kWh"} for call in calls)
            assert calls[0][4] == {"sum"}
            assert calls[1][4] == {"sum", "state"}
            assert calls[1][:2] == (
                chunk_start - timedelta(hours=1),
                chunk_end + timedelta(hours=1),
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "basis", [None, "statistics_sum", "solar_history_chunk", "energy_history_chunk"]
)
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
