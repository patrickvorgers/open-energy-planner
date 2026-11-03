"""Authenticated annual history ingestion, immutable counter storage and live plan evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession

from open_energy_planner.home_assistant.observation_wire import parse_configuration
from open_energy_planner.storage.counters import CounterStore
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_app import running
from tests.app.planning.test_live_app import wait_ready
from tests.database.support.archive import corrupt_document, read_current, read_document
from tests.ha_contract.test_observation_inputs import (
    configuration,
    configuration_document,
    report_body,
)


async def prepare(session, url, *, draft=False, multiple=False, statistics=True, reset=(1, 1)):
    """Configure export and a UTC bonus with the annual or settlement history transport."""
    now = datetime.now(UTC)
    body = configuration_document(configuration())
    body["bindings"].append(
        {"key": "grid.export_energy", "quantity": "energy", "entity_id": "sensor.export"}
    )
    if multiple:
        body["bindings"].append(
            {
                "key": "grid.export_energy.second",
                "quantity": "energy",
                "entity_id": "sensor.export2",
            }
        )
    async with session.put(url + "/v1/sources", json=body) as response:
        assert response.status == 200
        state = await response.json()
    config = parse_configuration(state["measurement_config"], revision=state["config_revision"])
    body = settings()
    body["bonuses"] = [
        {
            "rule_id": "solar",
            "effective_from": body["periods"][0]["effective_from"],
            "rate_formula": "0.04",
            "cap_kwh": 100000,
            "timezone": "UTC",
            "reset_month": reset[0],
            "reset_day": reset[1],
        }
    ]
    method, path = (
        (session.post, "/v1/contracts/preview") if draft else (session.put, "/v1/contracts")
    )
    async with method(url + path, json=body) as response:
        assert response.status == 200
    async with session.get(url + "/v1/snapshot") as response:
        state = await response.json()
    request = state["bonus_history"]["request"]
    start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
    count = int((end - start).total_seconds() / 3600)
    # Fractional absolute offsets keep an annual transfer above the default 256 KiB
    # while every successive difference is independently and exactly 2 kWh.
    offset = 123456.123456789
    hours = [
        [(start + timedelta(hours=index)).timestamp(), offset + 2 * index, offset + 2 * index]
        for index in range(count)
    ]
    report = report_body(config, now=now)
    if multiple:
        for reading in report["readings"]:
            if reading["key"] == "grid.export_energy":
                reading.update(value=hours[-1][2] * 0.6 + 1, unit="kWh")
            elif reading["key"] == "grid.export_energy.second":
                reading.update(value=hours[-1][2] * 0.4 + 2, unit="kWh")
    else:
        report["readings"][-1].update(value=hours[-1][2] + 3, unit="kWh")
    async with session.post(url + "/v1/observations", json=report) as response:
        assert response.status == 200
    if not statistics:
        return request, {
            "request_version": request["version"],
            "entity_id": request["entity_id"],
            "unit": "kWh",
            "hours": hours,
            **(
                {
                    "component_meters": [
                        ["sensor.export", hours[-1][2] * 0.6],
                        ["sensor.export2", hours[-1][2] * 0.4],
                    ]
                }
                if multiple
                else {}
            ),
        }
    factors = (
        (("sensor.export", 0.6), ("sensor.export2", 0.4)) if multiple else (("sensor.export", 1),)
    )
    return request, {
        "request_version": request["version"],
        "statistics": [
            {
                "statistic_id": entity,
                "metadata_id": index + 1,
                "source": "recorder",
                "unit": "kWh",
                "hours": [[row[0], row[1] * factor, row[2] * factor] for row in hours],
            }
            for index, (entity, factor) in enumerate(factors)
        ],
    }


def test_annual_history_over_http_persists_seed_and_supplies_measured_plan_budget(tmp_path):
    """Accept a bounded annual transfer. 2 kWh/hour without a physical live tail becomes the planner's actual starting quota."""

    async def scenario():
        """Exercise authentication, the dedicated body limit, native solving and retained immutable archive."""
        async with running(tmp_path, with_export=True) as (session, url):
            request, body = await prepare(session, url)
            if datetime.now(UTC).month >= 6:
                assert len(json.dumps(body)) > 256 * 1024
            async with (
                ClientSession() as anonymous,
                anonymous.post(url + "/v1/history", json=body) as response,
            ):
                assert response.status == 401
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 200, await response.text()
                assert (await response.json())["bonus_history"]["request"] is None
            book = read_current(tmp_path, CounterStore)
            expected = 2 * (len(body["statistics"][0]["hours"]) - 1)
            assert book.seeds[0].used_kwh == expected
            assert book.seeds[0].observed_at.isoformat() == request["end"]
            plan = (await wait_ready(session, url))["plan"]
            assert plan["input_snapshot"]["bonus_evidence"][0]["initial_kwh"] == expected
            archive = read_document(tmp_path, "bonus-counters", f"{book.version}.json")
            async with session.get(url + "/v1/contracts/budgets") as response:
                budget = (await response.json())["budgets"][0]
                assert budget["opening"]["source"] == "home_assistant_history"
                assert budget["opening"]["used_kwh"] == 0
                assert budget["statistics"]["reference_at"] == request["periods"][0][1]
                assert budget["statistics"]["latest_at"] == request["end"]
                assert budget["export_since_opening_kwh"] == pytest.approx(expected)
                assert budget["confirmation"] is None
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 409
        assert read_current(tmp_path, CounterStore).version == book.version
        assert read_document(tmp_path, "bonus-counters", f"{book.version}.json") == archive

    asyncio.run(scenario())


def test_invalid_history_preserves_manual_fallback_and_old_requests_cannot_overwrite_it(tmp_path):
    """Missing endpoints, wrong units and unavailable Recorder never fabricate zero. A confirmed seed wins over delayed data."""

    async def scenario():
        """Use real local endpoints and persist a manual seed after three explicit history failures."""
        async with running(tmp_path) as (session, url):
            request, body = await prepare(session, url)
            for changed in (
                {"unit": "W"},
                {"hours": body["statistics"][0]["hours"][:-1]},
            ):
                async with session.post(
                    url + "/v1/history",
                    json={**body, "statistics": [{**body["statistics"][0], **changed}]},
                ) as response:
                    assert response.status == 400
                assert read_current(tmp_path, CounterStore) is None
            async with session.post(
                url + "/v1/history",
                json={"request_version": request["version"], "error": "recorder_unavailable"},
            ) as response:
                assert response.status == 200
                assert (await response.json())["bonus_history"]["issue"] == "recorder_unavailable"
            manual = {
                "seeds": [
                    {
                        "rule_id": "solar",
                        "period_start": request["periods"][0][1],
                        "used_kwh": 99,
                        "meter_kwh": body["statistics"][0]["hours"][-1][2],
                        "entity_id": "sensor.export",
                        "observed_at": request["end"],
                    }
                ]
            }
            async with session.put(url + "/v1/bonus-counters", json=manual) as response:
                assert response.status == 200
                saved = (await response.json())["version"]
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 409
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"]["request"] is not None
            assert read_current(tmp_path, CounterStore).version == saved

    asyncio.run(scenario())


def test_unreadable_counter_pointer_requires_explicit_recovery_not_automatic_history(tmp_path):
    """A corrupt pointer blocks import and preserves a confirmed 99 kWh manual archive until explicit recovery."""

    async def scenario():
        """Restart with an unreadable pointer. Reject delayed history, then restore only through manual confirmation."""
        async with running(tmp_path) as (session, url):
            request, body = await prepare(session, url)
            manual = {
                "seeds": [
                    {
                        "rule_id": "solar",
                        "period_start": request["periods"][0][1],
                        "used_kwh": 99,
                        "meter_kwh": body["statistics"][0]["hours"][-1][2],
                        "entity_id": "sensor.export",
                        "observed_at": request["end"],
                    }
                ]
            }
            async with session.put(url + "/v1/bonus-counters", json=manual) as response:
                assert response.status == 200
                version = (await response.json())["version"]
        archive = tmp_path / "bonus-counters" / f"{version}.json"
        original = read_document(tmp_path, "bonus-counters", archive.name)
        pointer = tmp_path / "bonus-counters" / "current.json"
        corrupt_document(tmp_path, "bonus-counters", pointer.name, "broken pointer")
        async with running(tmp_path) as (session, url):
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"] == {
                    "request": None,
                    "issue": "bonus_counter_cache_unreadable",
                }
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 409
            assert read_document(tmp_path, "bonus-counters", pointer.name) == "broken pointer"
            assert read_document(tmp_path, "bonus-counters", archive.name) == original
            async with session.get(url + "/v1/bonus-counters") as response:
                assert (await response.json())["issue"] == "bonus_counter_cache_unreadable"
            async with session.put(url + "/v1/bonus-counters", json=manual) as response:
                assert response.status == 200
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"]["request"] is not None
        assert read_document(tmp_path, "bonus-counters", archive.name) == original
        assert read_current(tmp_path, CounterStore).seeds[0].used_kwh == 99

    asyncio.run(scenario())


@pytest.mark.parametrize("multiple", [False, True])
def test_draft_budget_reads_history_without_publishing_contract_or_counter(tmp_path, multiple):
    """Before save, complete annual statistics export history yields the draft budget. Neither rules nor counters reach storage."""
    from open_energy_planner.contracts.storage import ContractStore
    from tests.database.support.archive import list_documents

    async def scenario():
        """Use the real snapshot/history protocol, including multiple component meters and stable preview polling."""
        async with running(tmp_path, with_export=True) as (session, url):
            request, history = await prepare(session, url, draft=True, multiple=multiple)
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "solar",
                    "effective_from": body["periods"][0]["effective_from"],
                    "rate_formula": "0.04",
                    "cap_kwh": 100000,
                    "timezone": "UTC",
                }
            ]
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                result = await response.json()
                assert result["bonus_history_pending"]
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"]["request"]["version"] == request[
                    "version"
                ]
            async with session.post(url + "/v1/history", json=history) as response:
                assert response.status == 200, await response.text()
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                result = await response.json()
            budget = result["bonus_budget_preview"][0]
            expected = 2 * (len(history["statistics"][0]["hours"]) - 1)
            assert budget["used_kwh"] == pytest.approx(expected)
            assert budget["remaining_kwh"] == pytest.approx(100000 - expected)
            assert budget["measured_at"] is not None and budget["issue"] is None
            assert not result["bonus_history_pending"]
            body["bonuses"][0]["cap_kwh"] = 200000
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                assert (await response.json())["bonus_budget_preview"][0][
                    "remaining_kwh"
                ] == pytest.approx(200000 - expected)
            assert read_current(tmp_path, ContractStore) is None
            assert read_current(tmp_path, CounterStore) is None
            assert list_documents(tmp_path, "contracts") == []
            assert list_documents(tmp_path, "bonus-counters") == []

    asyncio.run(scenario())


def test_incomplete_draft_history_stays_unknown_and_cannot_replace_saved_evidence(tmp_path):
    """A missing latest endpoint cannot become zero usage or publish draft counters."""

    async def scenario():
        """Remove the required latest endpoint from a genuine draft request."""
        async with running(tmp_path, with_export=True) as (session, url):
            _request, history = await prepare(session, url, draft=True)
            del history["statistics"][0]["hours"][-1]
            async with session.post(url + "/v1/history", json=history) as response:
                assert response.status == 400
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "solar",
                    "effective_from": body["periods"][0]["effective_from"],
                    "rate_formula": "0.04",
                    "cap_kwh": 100000,
                    "timezone": "UTC",
                }
            ]
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                result = await response.json()
            assert result["bonus_budget_preview"][0]["used_kwh"] is None
            assert result["bonus_budget_preview"][0]["issue"] == "incomplete_or_invalid_history"
            assert not result["bonus_history_pending"]
            assert read_current(tmp_path, CounterStore) is None

    asyncio.run(scenario())


def test_saved_history_has_priority_and_changed_draft_rejects_late_reply(tmp_path):
    """A new draft cannot displace a pending saved-rule history read. Changing its reset rejects the old draft reply."""

    async def scenario():
        """Exercise both destinations through the same existing authenticated history route."""
        async with running(tmp_path, with_export=True) as (session, url):
            saved_request, saved_history = await prepare(session, url)
            draft = settings()
            draft["bonuses"] = [
                {
                    "rule_id": "draft-only",
                    "effective_from": draft["periods"][0]["effective_from"],
                    "rate_formula": "0.05",
                    "cap_kwh": 100000,
                    "timezone": "UTC",
                }
            ]
            async with session.post(url + "/v1/contracts/preview", json=draft) as response:
                assert response.status == 200
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"]["request"][
                    "version"
                ] == saved_request["version"]
            async with session.post(url + "/v1/history", json=saved_history) as response:
                assert response.status == 200
                pending = (await response.json())["bonus_history"]["request"]
            saved_book = read_current(tmp_path, CounterStore)
            draft["bonuses"][0]["reset_month"] = 2
            async with session.post(url + "/v1/contracts/preview", json=draft) as response:
                assert response.status == 200
            async with session.post(
                url + "/v1/history",
                json={"request_version": pending["version"], "error": "history_unavailable"},
            ) as response:
                assert response.status == 409
            assert read_current(tmp_path, CounterStore).version == saved_book.version

    asyncio.run(scenario())


def test_unchanged_saved_contract_preview_cannot_divert_confirmed_history(tmp_path, monkeypatch):
    """Even when frozen time makes saved and preview versions equal, the first report persists the saved counter baseline."""
    import open_energy_planner.app as app_module
    import open_energy_planner.contracts.api as contract_api_module
    from open_energy_planner.contracts.storage import ContractStore

    async def scenario():
        """Freeze parsing to the saved recorded instant to force the otherwise rare exact-version collision."""
        async with running(tmp_path, with_export=True) as (session, url):
            request, history = await prepare(session, url)
            stored = read_current(tmp_path, ContractStore)

            class FixedDateTime(datetime):
                """Give saved and draft requests exactly the same revision identity."""

                @classmethod
                def now(cls, tz=None):
                    """Return the precise saved instant without altering history's normalized boundaries."""
                    return stored.recorded_at.astimezone(tz)

            monkeypatch.setattr(app_module, "datetime", FixedDateTime)
            monkeypatch.setattr(contract_api_module, "datetime", FixedDateTime)
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "solar",
                    "effective_from": body["periods"][0]["effective_from"],
                    "rate_formula": "0.04",
                    "cap_kwh": 100000,
                    "timezone": "UTC",
                }
            ]
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                assert (await response.json())["version"] == stored.version
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["bonus_history"]["request"]["version"] == request[
                    "version"
                ]
            async with session.post(url + "/v1/history", json=history) as response:
                assert response.status == 200
            assert read_current(tmp_path, CounterStore).seeds[0].used_kwh == 2 * (
                len(history["statistics"][0]["hours"]) - 1
            )

    asyncio.run(scenario())
