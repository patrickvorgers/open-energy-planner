"""Read-only Planning projections avoid work that belongs to another disclosure."""

import asyncio
import json
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict

import pytest
from aiohttp import ClientSession

from open_energy_planner.savings import api
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.contract.test_contract_app import running
from tests.app.savings.support.scenarios import START, problem


def test_plan_and_simulation_reads_skip_evidence_and_preserve_originals(tmp_path, monkeypatch):
    """Each projection retains identical original values while skipping evidence until explicitly requested."""
    plan = {
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(
                    asdict(problem()), default=lambda datetime_value: datetime_value.isoformat()
                )
            ),
            "tariff_evidence": [
                {
                    "start": START.isoformat(),
                    "end": "2026-10-22T10:15:00+00:00",
                    "market_price": 0.1,
                    "buy_price": 0.2,
                    "sell_price": 0.1,
                    "price_kind": "published",
                    "native_resolution_minutes": 15,
                    "source_version": "synthetic-archived-price-version",
                }
            ],
        },
        "expected_flows": [
            {
                "grid_import": 1,
                "grid_export": 0,
                "batteries": [{"device_id": "battery", "end_kwh": 2, "discharge_kwh": 0}],
            }
        ],
        "plan_id": "projection-original",
        "generated_at": START.isoformat(),
        "reference_forecast": {"private_replay": "synthetic-large-archive"},
        "solar_evidence": {"roof": {"training": list(range(1000))}},
        "currency": "EUR",
    }
    before = deepcopy(plan)
    with closing(SqlPlanRepository(tmp_path)) as repository:
        repository.state["plan"] = plan
        repository.save()

    calls = []
    original_evidence, original_scenarios, original_latest = (
        api.evidence,
        api.scenarios,
        api.PlanValuations.latest,
    )

    def evidence(original):
        """Observe actual evidence construction without replacing its output."""
        calls.append("evidence")
        return original_evidence(original)

    def scenarios(*arguments):
        """Observe actual scenario replay without changing the financial oracle."""
        calls.append("scenarios")
        return original_scenarios(*arguments)

    def latest(*arguments):
        """Observe repricing reads that neither plan nor evidence projection needs."""
        calls.append("valuation")
        return original_latest(*arguments)

    monkeypatch.setattr(api, "evidence", evidence)
    monkeypatch.setattr(api, "scenarios", scenarios)
    monkeypatch.setattr(api.PlanValuations, "latest", latest)

    async def check():
        """Use the real authenticated routes and compare projections with the original complete response."""
        async with running(tmp_path) as (session, url):
            path = url + "/v1/plans/projection-original/comparison"
            async with session.get(path) as response:
                assert response.status == 200
                complete = await response.json()
            for view, expected_calls in (
                ("plan", []),
                ("simulations", ["valuation", "scenarios"]),
                ("evidence", ["evidence"]),
            ):
                calls.clear()
                async with (
                    ClientSession() as anonymous,
                    anonymous.get(path, params={"view": view}) as response,
                ):
                    assert response.status == 401
                async with session.get(path, params={"view": view}) as response:
                    assert response.status == 200
                    assert response.headers["Cache-Control"] == "no-store"
                    projected = await response.json()
                assert calls == expected_calls
                if view == "plan":
                    expected = {
                        key: value
                        for key, value in complete["plan"].items()
                        if key
                        not in ("reference_forecast", "solar_evidence", "forecast_quality_names")
                    }
                    expected["input_snapshot"] = {
                        **expected["input_snapshot"],
                        "tariff_evidence": [
                            {
                                key: value
                                for key, value in before["input_snapshot"]["tariff_evidence"][
                                    0
                                ].items()
                                if key != "source_version"
                            }
                        ],
                    }
                    assert projected == {"plan": expected}
                    assert len(json.dumps(projected)) < len(json.dumps(complete["plan"]))
                elif view == "simulations":
                    assert projected == {
                        "plan_id": "projection-original",
                        **{
                            key: value
                            for key, value in complete.items()
                            if key not in ("plan", "evidence")
                        },
                    }
                else:
                    assert projected == {
                        "plan_id": "projection-original",
                        "evidence": complete["evidence"],
                    }
            for query in ({"view": "unknown"}, {"view": "plan", "start": START.isoformat()}):
                async with session.get(path, params=query) as response:
                    assert response.status == 400
            async with session.get(url + "/v1/plans/projection-original") as response:
                assert await response.json() == before
            async with session.get(
                url + "/v1/plans/missing/comparison", params={"view": "evidence"}
            ) as response:
                assert response.status == 404

    asyncio.run(check())


@pytest.mark.parametrize("context", ["complete", "legacy", "empty_intervals"])
def test_real_evidence_projection_returns_cards_or_an_explicit_issue(tmp_path, context):
    """The actual producer returns populated evidence or an owned issue for supported incomplete archives."""
    plan = {
        "plan_id": "evidence-contract",
        "generated_at": START.isoformat(),
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(
                    asdict(problem()), default=lambda datetime_value: datetime_value.isoformat()
                )
            )
        },
    }
    if context == "legacy":
        plan.pop("input_snapshot")
    elif context == "empty_intervals":
        plan["input_snapshot"]["problem"]["intervals"] = []
    with closing(SqlPlanRepository(tmp_path)) as repository:
        repository.state["plan"] = plan
        repository.save()

    async def check():
        """Exercise the authenticated HTTP projection without mocking evidence or its response."""
        async with running(tmp_path) as (session, url):
            async with session.get(
                url + "/v1/plans/evidence-contract/comparison?view=evidence"
            ) as response:
                assert response.status == 200
                result = await response.json()
            assert result["plan_id"] == result["evidence"]["plan_id"] == "evidence-contract"
            evidence = result["evidence"]
            if context == "complete":
                assert evidence["summary"] and evidence["intervals"]
                assert not evidence.get("issue")
            else:
                assert evidence["summary"] == evidence["intervals"] == []
                assert (
                    evidence["issue"]
                    == "Original plan evidence is incomplete or unavailable for this plan."
                )

    asyncio.run(check())
