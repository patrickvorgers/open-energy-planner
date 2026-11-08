"""Reuse validated meter cohorts for settings changes without weakening replacement or fault boundaries."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from threading import get_ident

import pytest

from open_energy_planner.solar.wire import configuration_document
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.solar.history_support import HISTORY_NOW as NOW
from tests.app.solar.history_support import chunk_report, discover, finish, runtime_case
from tests.app.solar.support import SITE, fixture_weather, weather


def test_http_settings_save_reuses_validated_years_without_event_loop_chunk_reads(
    tmp_path, monkeypatch
):
    """Geometry, unrestricted-period and preference saves retain years of accepted data without rescanning them on the loop."""
    seeded = runtime_case(tmp_path)
    discover(seeded)
    finish(seeded)
    expected_hours = len(seeded.histories["sensor.pv0"].hours)
    assert expected_hours > 26000

    async def scenario():
        """Save through the real app lock/persistence/install path and observe only history-row reads."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            initial = configuration_document(seeded.configuration)
            initial["expected_version"] = initial["version"]
            await configure_forecasts(session, url, solar_definition=initial)
            async with session.get(url + "/v1/solar") as response:
                original = await response.json()
            current = original["configuration"]
            assert original["history_import"]["sensor.pv0"]["hours"] == expected_hours
            loop_thread = get_ident()
            reads = []
            read = SqlDocuments.read

            def record(documents, name):
                """Detect causal annual-row scans during the caller-held install callback, without timing thresholds."""
                if name.startswith("history-import-chunk-"):
                    reads.append((name, get_ident() == loop_thread))
                return read(documents, name)

            monkeypatch.setattr(SqlDocuments, "read", record)
            for change in (
                {"tilt": 45},
                {
                    "can_curtail": True,
                    "unrestricted_history_start": "2024-01-01T00:00:00+00:00",
                    "unrestricted_history_end": "2025-12-31T00:00:00+00:00",
                },
                {"forecast_source": "pvwatts"},
            ):
                body = {
                    "sources": [{**current["sources"][0], **change}],
                    "inverters": current["inverters"],
                    "expected_version": current["version"],
                }
                async with session.put(url + "/v1/solar", json=body) as response:
                    assert response.status == 200, await response.text()
                    updated = await response.json()
                assert reads == [], "Unrelated settings rescanned accepted annual meter rows"
                assert updated["history_import"]["sensor.pv0"]["hours"] == expected_hours
                assert updated["history_import"]["sensor.pv0"]["loaded_years"] == [
                    2023,
                    2024,
                    2025,
                    2026,
                ]
                current = updated["configuration"]
                async with session.put(url + "/v1/solar", json=body) as response:
                    assert response.status == 409
                assert reads == []

    asyncio.run(scenario())


@pytest.mark.parametrize("hard_fault", [False, True])
def test_same_cohort_settings_retain_history_fault_and_retry(tmp_path, hard_fault):
    """An unrelated definition change cannot clear an unchanged meter's retry or durable continuity quarantine."""
    runtime = runtime_case(tmp_path)
    if hard_fault:
        discover(runtime, first="2026-01-01T00:00:00+00:00")
        finish(runtime)
        now = NOW + timedelta(days=1)
        pending = runtime.request(now)
        conflict = chunk_report(
            pending, identity={"metadata_id": 99, "source": "recorder", "unit": "kWh"}
        )
        runtime.receive(pending["version"], conflict.history, now, report=conflict)
        expected = "history_continuity_unavailable"
    else:
        now = NOW
        pending = runtime.request(now)
        runtime.receive(pending["version"], None, now, error="history_unavailable")
        expected = "history_unavailable"
    histories = runtime.histories.copy()
    retry = runtime.history_retry_after.copy()
    configuration = replace(
        runtime.configuration, sources=(replace(runtime.configuration.sources[0], tilt=45),)
    )
    runtime.store.save(configuration)
    runtime.configure(configuration)
    assert runtime.histories == histories
    assert runtime.history_issues["sensor.pv0"] == expected
    assert runtime.history_retry_after == retry
    assert runtime.request(now) is None
    if hard_fault:
        assert runtime.history_import.issues["sensor.pv0"] == expected


def test_new_source_binding_revalidates_and_new_meter_cannot_inherit_old_history(
    tmp_path, monkeypatch
):
    """A new source ID revalidates its retained meter; a replacement meter starts discovery and revokes old worker outputs."""
    runtime = runtime_case(tmp_path)
    discover(runtime, first="2026-01-01T00:00:00+00:00")
    finish(runtime)
    accepted = runtime.histories["sensor.pv0"]
    chunks = []
    chunk = runtime.history_import._chunk

    def record(state, descriptor):
        """Require the existing full validator whenever the source binding changes."""
        chunks.append(state["entity_id"])
        return chunk(state, descriptor)

    monkeypatch.setattr(runtime.history_import, "_chunk", record)
    renamed = replace(
        runtime.configuration,
        sources=(replace(runtime.configuration.sources[0], source_id="replacement"),),
    )
    runtime.store.save(renamed)
    runtime.configure(renamed)
    assert chunks == ["sensor.pv0"]
    assert runtime.histories["sensor.pv0"] == accepted
    assert runtime.histories["sensor.pv0"] is not accepted
    runtime.set_weather(SITE, weather(NOW))
    queued = runtime.job(NOW)
    replaced = replace(
        renamed, sources=(replace(renamed.sources[0], entity_id="sensor.replacement"),)
    )
    runtime.store.save(replaced)
    runtime.configure(replaced)
    assert runtime.histories == {}
    assert runtime.request(NOW)["entity_id"] == "sensor.replacement"
    assert runtime.request(NOW)["basis"] == "solar_history_range"
    assert not runtime.install((queued[0],), NOW)
