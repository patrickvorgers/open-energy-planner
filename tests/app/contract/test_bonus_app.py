"""Authenticated bonus configuration, measured counters and real solver publication."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession

from open_energy_planner.storage.counters import CounterStore
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_app import running
from tests.app.planning.test_live_app import wait_ready
from tests.database.support.archive import read_current, read_plan
from tests.ha_contract.test_observation_inputs import (
    configuration,
    configuration_document,
    parse_configuration,
    report_body,
)


def test_bonus_settings_counter_seed_and_plan_over_http(tmp_path):
    """Require confirmed budget, publish 0.4*0.04=0.016 bonus, and preserve counters across replans/restart."""

    async def scenario():
        """Use a real local server and HiGHS without external prices, HA devices or provider control."""
        async with running(tmp_path, with_export=True) as (session, url):
            now = datetime.now(UTC)
            body = configuration_document(configuration())
            body["bindings"].append(
                {"key": "grid.export_energy", "quantity": "energy", "entity_id": "sensor.export"}
            )
            async with session.put(url + "/v1/sources", json=body) as response:
                assert response.status == 200
                state = await response.json()
            config = parse_configuration(
                state["measurement_config"], revision=state["measurement_config"]["revision"]
            )
            observation = report_body(config, now=datetime.now(UTC))
            observation["readings"][-1].update(value=50, unit="kWh")
            async with session.post(url + "/v1/observations", json=observation) as response:
                assert response.status == 200
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "solar",
                    "effective_from": now.replace(
                        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                    ).isoformat(),
                    "rate_formula": "0.04",
                    "cap_kwh": 100,
                    "timezone": "UTC",
                }
            ]
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                preview = await response.json()
                assert preview["bonus_preview"][0]["samples"][0]["extra_rate"] == 0.04
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
                assert state["planning"]["reason"] == "bonus_counter_baseline_required"
                assert state["plan"] is None
            seed = {
                "expected_version": None,
                "seeds": [
                    {
                        "rule_id": "solar",
                        "period_start": now.replace(
                            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                        ).isoformat(),
                        "used_kwh": 99.6,
                        "meter_kwh": 50,
                        "entity_id": "sensor.export",
                        "observed_at": (now - timedelta(seconds=1)).isoformat(),
                    }
                ],
            }
            from open_energy_planner.contracts.bonus_statistics import (
                StatisticReference,
                StatisticsBasis,
            )

            endpoint = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            seed["seeds"][0]["observed_at"] = endpoint.isoformat()
            seed["seeds"][0]["component_meters"] = [["sensor.export", 50]]
            seed["seeds"][0]["statistics"] = StatisticsBasis(
                endpoint,
                endpoint,
                (StatisticReference("sensor.export", 1, "recorder", "kWh", 50, 50),),
                99.6,
                now,
                "a" * 64,
                True,
            ).document()
            async with session.put(url + "/v1/bonus-counters", json=seed) as response:
                assert response.status == 400
            async with session.get(url + "/v1/bonus-counters") as response:
                assert (await response.json())["version"] is None
            async with session.get(url + "/v1/snapshot") as response:
                snapshot = await response.json()
                assert snapshot["plan"] is None
                history_request = snapshot["bonus_history"]["request"]
            endpoint = datetime.fromisoformat(history_request["end"])
            async with session.post(
                url + "/v1/history",
                json={
                    "request_version": history_request["version"],
                    "statistics": [
                        {
                            "statistic_id": "sensor.export",
                            "metadata_id": 1,
                            "source": "recorder",
                            "unit": "kWh",
                            "hours": [[(endpoint - timedelta(hours=1)).timestamp(), 50, 50]],
                        }
                    ],
                },
            ) as response:
                assert response.status == 200
            async with session.get(url + "/v1/contracts/budgets") as response:
                pending = await response.json()
            async with session.post(
                url + "/v1/contracts/budgets",
                json={
                    "rule_id": "solar",
                    "used_kwh": 99.6,
                    "contract_version": pending["contract_version"],
                    **pending["budgets"][0]["confirmation"],
                },
            ) as response:
                assert response.status == 200
            async with session.get(url + "/v1/bonus-counters") as response:
                saved = await response.json()
            seed["seeds"][0].pop("statistics")
            async with session.get(url + "/v1/contracts/budgets") as response:
                budgets = await response.json()
                assert budgets["budgets"][0]["used_kwh"] == 99.6
                assert abs(budgets["budgets"][0]["remaining_kwh"] - 0.4) < 1e-10
            state = await wait_ready(session, url)
            plan = state["plan"]
            assert abs(plan["independent_costs"]["bonus_income"] - 0.016) < 1e-7
            assert (
                plan["input_snapshot"]["bonus_evidence"][0]["counter_version"] == saved["version"]
            )
            assert sum(plan["expected_bonuses"][0]["quota_kwh"]) == pytest.approx(0.4)
            archive = plan["plan_id"]
            original = read_plan(tmp_path, archive)
            async with session.put(url + "/v1/bonus-counters", json=seed) as response:
                assert response.status == 409
            for _ in range(2):
                observation["captured_at"] = datetime.now(UTC).isoformat()
                async with session.post(url + "/v1/observations", json=observation) as response:
                    assert response.status == 200
            async with session.get(url + "/v1/bonus-counters") as response:
                assert (await response.json())["version"] == saved["version"]
            assert read_plan(tmp_path, archive) == original
            async with ClientSession() as anonymous:
                for method in (anonymous.get, anonymous.put):
                    async with method(url + "/v1/bonus-counters") as response:
                        assert response.status == 401
        assert read_current(tmp_path, CounterStore).version == saved["version"]

    asyncio.run(scenario())


def test_budget_confirmation_uses_fixed_statistics_reference_despite_physical_updates(tmp_path):
    """Confirm only a missing 25-kWh prefix after live physical updates. Reject changed references and duplicate writes."""
    from tests.app.history.test_history_app import prepare

    async def scenario():
        """Use the real authenticated app with two late sum endpoints and concurrent confirmation attempts."""
        async with running(tmp_path, with_export=True) as (session, url):
            request, history = await prepare(session, url)
            rows = history["statistics"][0]["hours"][-2:]
            statistics = {
                "request_version": request["version"],
                "statistics": [
                    {
                        "statistic_id": "sensor.export",
                        "metadata_id": 1,
                        "source": "recorder",
                        "unit": "kWh",
                        "hours": rows,
                    }
                ],
            }
            async with session.post(url + "/v1/history", json=statistics) as response:
                assert response.status == 200, await response.text()
            async with session.get(url + "/v1/contracts/budgets") as response:
                saved = await response.json()
            budget = saved["budgets"][0]
            assert budget["used_kwh"] is None
            assert read_current(tmp_path, CounterStore).seeds[0].used_kwh is None
            assert (
                budget["statistics"]["reference_at"]
                == datetime.fromtimestamp(rows[0][0] + 3600, UTC).isoformat()
            )
            body = {
                "rule_id": "solar",
                "used_kwh": 25,
                "contract_version": saved["contract_version"],
                **budget["confirmation"],
            }
            for changes, status in (
                ({"reference_version": "stale"}, 409),
                ({"config_revision": "stale"}, 409),
                ({"contract_version": "stale"}, 409),
                ({"used_kwh": -1}, 400),
                ({"used_kwh": 10**400}, 400),
                ({"used_kwh": True}, 400),
                ({"meter_kwh": 0}, 400),
            ):
                async with session.post(
                    url + "/v1/contracts/budgets", json=body | changes
                ) as response:
                    assert response.status == status
            async with session.get(url + "/v1/snapshot") as response:
                config_body = (await response.json())["measurement_config"]
            config = parse_configuration(config_body, revision=config_body["revision"])
            observation = report_body(config, now=datetime.now(UTC))
            observation["readings"][-1].update(value=999999, unit="kWh")
            async with session.post(url + "/v1/observations", json=observation) as response:
                assert response.status == 200
            responses = await asyncio.gather(
                *(session.post(url + "/v1/contracts/budgets", json=body) for _ in range(2))
            )
            assert sorted(response.status for response in responses) == [200, 409]
            for response in responses:
                await response.read()
                response.release()
            async with session.get(url + "/v1/contracts/budgets") as response:
                budget = (await response.json())["budgets"][0]
            assert budget["used_kwh"] == 27
            assert budget["opening"]["used_kwh"] == 25
            assert budget["opening"]["source"] == "manual_confirmation"
            assert budget["export_since_opening_kwh"] == 2
            assert budget["confirmation"] is None
        book = read_current(tmp_path, CounterStore)
        assert book.seeds[0].statistics.prefix_kwh == 25
        async with (
            running(tmp_path, with_export=True) as (session, url),
            session.get(url + "/v1/contracts/budgets") as response,
        ):
            assert (await response.json())["budgets"][0]["used_kwh"] == 27

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,missing_prefix",
    [
        ("rewrite", False),
        ("invalid_transport", False),
        ("history_missing", False),
        ("unsupported_statistics", False),
        ("recorder_unavailable", False),
        ("history_missing", True),
    ],
)
def test_history_revalidation_failure_blocks_persisted_budget_and_recovers(
    tmp_path, monkeypatch, failure, missing_prefix
):
    """Missing or invalid evidence persists an unknown budget. Transient Recorder outages retain only the fresh checkpoint."""
    from copy import deepcopy

    import open_energy_planner.app as app_module
    import open_energy_planner.contracts.api as contract_api_module
    from tests.app.history.test_history_app import prepare

    async def scenario():
        """Advance one hour through real HTTP without changing the retained reference or deleting old books."""
        async with running(tmp_path, with_export=True) as (session, url):
            # Keep this refresh within one annual period even across New Year's midnight.
            reset = (datetime.now(UTC).month % 12 + 1, 15)
            request, history = await prepare(session, url, reset=reset)
            if missing_prefix:
                history["statistics"][0]["hours"] = history["statistics"][0]["hours"][-2:]
            body = {
                "request_version": request["version"],
                "statistics": [
                    {
                        "statistic_id": "sensor.export",
                        "metadata_id": 1,
                        "source": "recorder",
                        "unit": "kWh",
                        "hours": history["statistics"][0]["hours"],
                    }
                ],
            }
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 200
            original = read_current(tmp_path, CounterStore)
            if missing_prefix:
                async with session.get(url + "/v1/contracts/budgets") as response:
                    initial = await response.json()
                confirmation = {
                    "rule_id": "solar",
                    "used_kwh": 25,
                    "contract_version": initial["contract_version"],
                    **initial["budgets"][0]["confirmation"],
                }
            future = datetime.now(UTC) + timedelta(hours=1)

            class LaterDateTime(datetime):
                """Hold app time in the next statistics refresh hour."""

                @classmethod
                def now(cls, tz=None):
                    """Return the independently chosen next-hour instant."""
                    return future.astimezone(tz)

            monkeypatch.setattr(app_module, "datetime", LaterDateTime)
            monkeypatch.setattr(contract_api_module, "datetime", LaterDateTime)
            async with session.get(url + "/v1/snapshot") as response:
                pending = (await response.json())["bonus_history"]["request"]
            body["request_version"] = pending["version"]
            last = body["statistics"][0]["hours"][-1]
            body["statistics"][0]["hours"].append([last[0] + 3600, last[1] + 2, last[2] + 2])
            broken = deepcopy(body)
            if failure == "invalid_transport":
                broken["statistics"][0]["unit"] = "W"
            elif failure == "rewrite":
                broken["statistics"][0]["hours"][0][1] -= 1
            else:
                broken = {"request_version": pending["version"], "error": failure}
            async with session.post(url + "/v1/history", json=broken) as response:
                assert response.status == (400 if failure == "invalid_transport" else 200)
            rejected = read_current(tmp_path, CounterStore)
            permanent = failure != "recorder_unavailable"
            assert bool(rejected.seeds[0].statistics.rejected_digest) == permanent
            if not permanent:
                assert rejected.version == original.version
            async with session.get(url + "/v1/contracts/budgets") as response:
                budget = (await response.json())["budgets"][0]
                if permanent:
                    assert budget["used_kwh"] is None
                    assert budget["issue"] == "bonus_statistics_revalidation_required"
                else:
                    assert budget["used_kwh"] == original.seeds[0].used_kwh
                    assert budget["issue"] is None
                assert budget["confirmation"] is None
            if missing_prefix:
                async with session.post(
                    url + "/v1/contracts/budgets", json=confirmation
                ) as response:
                    assert response.status == 409
            async with session.get(url + "/v1/snapshot") as response:
                body["request_version"] = (await response.json())["bonus_history"]["request"][
                    "version"
                ]
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 200, await response.text()
            restored = read_current(tmp_path, CounterStore)
            if missing_prefix:
                assert restored.seeds[0].used_kwh is None
                async with session.post(
                    url + "/v1/contracts/budgets", json=confirmation
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["budgets"][0]["used_kwh"] == 29
            else:
                assert restored.seeds[0].statistics.used(future) == original.seeds[0].used_kwh + 2
            assert (
                restored.seeds[0].statistics.reference_version
                == original.seeds[0].statistics.reference_version
            )

    asyncio.run(scenario())


def test_budget_reset_in_nonexistent_local_time_is_an_explicit_row_issue(tmp_path, monkeypatch):
    """Cairo's skipped 2026-04-24 midnight makes one annual period unavailable, not the entire endpoint."""
    import open_energy_planner.app as app_module
    import open_energy_planner.contracts.api as contract_api_module

    class FixedDateTime(datetime):
        """Hold the actual reset year stable as future CI runs advance."""

        @classmethod
        def now(cls, tz=None):
            """Return a post-transition instant so April's nonexistent midnight is the annual boundary."""
            return datetime(2026, 5, 1, tzinfo=UTC).astimezone(tz)

    async def scenario():
        """Submit a valid dated rule, then exercise the actual timezone failure through HTTP."""
        async with running(tmp_path, with_export=True) as (session, url):
            monkeypatch.setattr(app_module, "datetime", FixedDateTime)
            monkeypatch.setattr(contract_api_module, "datetime", FixedDateTime)
            contract = settings()
            contract["bonuses"] = [
                {
                    "rule_id": "cairo",
                    "effective_from": "2026-01-01T00:00:00Z",
                    "rate_formula": "0.04",
                    "cap_kwh": 100,
                    "timezone": "Africa/Cairo",
                    "reset_month": 4,
                    "reset_day": 24,
                }
            ]
            async with session.put(url + "/v1/contracts", json=contract) as response:
                assert response.status == 200
            async with session.post(url + "/v1/contracts/preview", json=contract) as response:
                assert response.status == 200
                preview = await response.json()
                assert preview["bonus_budget_preview"][0]["period_start"] is None
                assert "nonexistent" in preview["bonus_budget_preview"][0]["issue"]
                assert not preview["bonus_history_pending"]
            async with session.get(url + "/v1/contracts/budgets") as response:
                assert response.status == 200
                budget = (await response.json())["budgets"][0]
                assert budget["period_start"] is None
                assert budget["used_kwh"] is None and budget["remaining_kwh"] is None
                assert budget["issue"] == "Annual reset falls in a nonexistent local time"

    asyncio.run(scenario())
