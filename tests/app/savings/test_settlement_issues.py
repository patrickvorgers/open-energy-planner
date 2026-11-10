"""Settlement failures expose bounded evidence guidance while retaining raw archives and status semantics."""

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings import api as savings_api
from open_energy_planner.savings.bonus import HistoricalBonuses
from open_energy_planner.savings.model import PendingSettlement
from open_energy_planner.savings.runtime import (
    EvaluationResult,
    EvaluationState,
    SavingsRuntime,
    settlement_activity_detail,
)
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS, PUBLISHED_PRICES
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame, problem

PRIVATE_EVIDENCE = (
    "postgresql://private-user:SECRET_PASSWORD@private.invalid/archive?token=SECRET_TOKEN"
)


def test_archive_maintenance_failure_does_not_report_a_financial_failure(
    tmp_path, monkeypatch, caplog
):
    """An isolated compression failure retains usable financial status and logs locations without secret exception values."""
    repository = SqlPlanRepository(tmp_path)

    def failed_compression(_documents):
        """Exercise the actual worker's unexpected maintenance-error boundary with private-value sentinels."""
        raise RuntimeError(PRIVATE_EVIDENCE)

    monkeypatch.setattr(savings_api, "recompress", failed_compression)

    async def observe_worker():
        """Wait for the real maintenance attempt rather than substituting financial execution."""
        app = web.Application()
        _, runtime, _ = savings_api.register(
            app,
            repository,
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(configuration=None),
            SimpleNamespace(configuration=None),
            asyncio.Lock(),
            SimpleNamespace(value=None),
            lambda: None,
            mutation_lock=asyncio.Lock(),
        )
        async with TestServer(app):
            async with asyncio.timeout(3):
                while not any(
                    record.name == savings_api.__name__
                    and record.getMessage().startswith("Archive maintenance failed")
                    for record in caplog.records
                ):
                    await asyncio.sleep(0.01)
            assert runtime.issue is None
            assert runtime.attention is False

    try:
        with caplog.at_level(logging.INFO, logger=savings_api.__name__):
            asyncio.run(observe_worker())
        assert "failed_compression" in caplog.text
        assert "SECRET_PASSWORD" not in caplog.text
        assert "SECRET_TOKEN" not in caplog.text
    finally:
        repository.close()


@pytest.mark.parametrize(
    "issue,cause,action",
    [
        (
            "Historical bonus baseline awaits captured export evidence",
            "cannot yet connect the historical interval",
            "Do not change a valid contract start date",
        ),
        (
            "Historical bonus interval predates the confirmed statistics reference",
            "before the confirmed bonus statistics reference",
            "Keep the confirmed bonus settings",
        ),
        (
            "Historical bonus history unavailable: history_missing",
            "endpoints are missing",
            "Check Recorder",
        ),
        (
            "Historical bonus baseline awaits completed hourly statistics",
            "hourly export statistics",
            "bonus budget under Contract",
        ),
        (
            "Historical bonus baseline is being read from Home Assistant",
            "still being read from Home Assistant",
            "next background check tries again",
        ),
        (
            "Historical bonus history unavailable: recorder_unavailable",
            "Recorder is unavailable",
            "Check Recorder",
        ),
        (
            "Historical bonus history unavailable: unsupported_statistics",
            "cannot supply supported statistics",
            "export statistics in Home Assistant",
        ),
        (
            "Historical bonus export evidence has a gap or changed meters",
            "gap or use different export meters",
            "export meters and bonus baseline under Contract",
        ),
        (
            "Historical bonus period does not start on a whole UTC hour. Confirm a manual baseline",
            "needs a confirmed manual starting total",
            "bonus baseline under Contract",
        ),
        (
            "Reference awaits metering across the installation change",
            "missing across an installation change",
            "Check that Home Assistant records",
        ),
        (
            PRIVATE_EVIDENCE,
            "required historical evidence is incomplete or invalid",
            "report this activity",
        ),
    ],
)
def test_savings_waiting_details_identify_safe_cause_progress_and_action(issue, cause, action):
    """Different waiting causes stay distinguishable while arbitrary evidence never becomes public text."""
    result = EvaluationResult(EvaluationState.PENDING, 1, 2, START.isoformat(), END.isoformat())
    detail = settlement_activity_detail(result, issue)
    assert "processed 1 of 2 measured intervals" in detail
    assert cause in detail and action in detail
    assert "Previous published financial results remain available" in detail
    assert PRIVATE_EVIDENCE not in detail
    if issue == PRIVATE_EVIDENCE:
        assert "exact missing or invalid item could not be identified" in detail


@pytest.mark.parametrize(
    "fault",
    [
        "plan_timestamp",
        "price_timestamp",
        "activation_timestamp",
        "pending",
        "missing_prices",
        "private_contract_reason",
        "private_continuity_reason",
    ],
)
def test_settlement_process_and_worker_keep_untrusted_evidence_out_of_status_and_logs(
    fault, tmp_path, monkeypatch, caplog
):
    """Real replay catches bad retained timestamps and distinguishes waiting from invalid financial evidence."""
    repository = SqlPlanRepository(tmp_path)
    selected_contract = contract()
    document = {
        "plan_id": "privacy-original-plan",
        "generated_at": START.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "input_snapshot": {
            "contract_version": selected_contract.version,
            "problem": json.loads(
                json.dumps(asdict(problem()), default=lambda value: value.isoformat())
            ),
        },
        "steps": [],
    }
    if fault == "plan_timestamp":
        document["input_snapshot"]["problem"]["intervals"][0]["start"] = PRIVATE_EVIDENCE
    repository.state["plan"] = document
    repository.save()
    store = SettlementStore(repository.engine)
    documents = SqlDocuments(repository.engine, "savings")
    with repository.engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(START), plan_id=document["plan_id"])
        )
        if fault == "activation_timestamp":
            connection.execute(
                ACTIVATIONS.insert().values(
                    activated_at=(START + timedelta(minutes=5)).isoformat() + PRIVATE_EVIDENCE,
                    plan_id=document["plan_id"],
                )
            )
        if fault != "missing_prices":
            retain(
                connection,
                (
                    PriceSeries(
                        "published",
                        "NL",
                        "EUR",
                        START - timedelta(hours=1),
                        None,
                        None,
                        (PricePoint(START, END, 0.1, PriceKind.PUBLISHED, 15),),
                    ),
                ),
            )
        if fault == "price_timestamp":
            connection.execute(PUBLISHED_PRICES.update().values(end=PRIVATE_EVIDENCE))
    store.put_frames((frame(),), {"bindings": bindings(), "seeds": [], "location": None})
    if fault in {"pending", "private_contract_reason", "private_continuity_reason"}:

        def await_private_evidence(_runtime, *_arguments):
            """Raise unrecognized pending or validation text without declaring retained values public."""
            if fault == "pending":
                raise PendingSettlement(PRIVATE_EVIDENCE)
            reason = (
                "Contract coverage is missing"
                if fault == "private_contract_reason"
                else "Reference continuity requires the missing interval first"
            )
            raise ValueError(reason + ": " + PRIVATE_EVIDENCE)

        monkeypatch.setattr(HistoricalBonuses, "prepare", await_private_evidence)
    runtime = SavingsRuntime(store, documents, repository)

    async def observe_worker():
        """Run the actual logging worker over the same archive without substituting financial processing."""
        app = web.Application()
        _, worker_runtime, _ = savings_api.register(
            app,
            repository,
            SimpleNamespace(current=selected_contract),
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(configuration=None),
            SimpleNamespace(configuration=None),
            asyncio.Lock(),
            SimpleNamespace(value=None),
            lambda: SiteLocation(52, 6, "UTC"),
            mutation_lock=asyncio.Lock(),
        )
        async with TestServer(app):
            async with asyncio.timeout(3):
                while not any(
                    record.name == savings_api.__name__
                    and record.getMessage().startswith("Settlement ")
                    for record in caplog.records
                ):
                    await asyncio.sleep(0.01)
        return worker_runtime

    try:
        with caplog.at_level(logging.INFO, logger="open_energy_planner.savings"):
            runtime.process(selected_contract, END + timedelta(hours=1))
            worker_runtime = asyncio.run(observe_worker())
        for processed in (runtime, worker_runtime):
            assert PRIVATE_EVIDENCE not in processed.issue
            assert "SECRET_PASSWORD" not in processed.issue
            assert "SECRET_TOKEN" not in processed.issue
            assert processed.attention is (
                fault
                in {
                    "plan_timestamp",
                    "price_timestamp",
                    "activation_timestamp",
                    "private_contract_reason",
                    "private_continuity_reason",
                }
            )
            if fault == "missing_prices":
                assert processed.issue == "Published price coverage is missing"
            elif fault == "pending":
                assert "waiting for complete validated evidence" in processed.issue
            else:
                assert "Retained settlement evidence is invalid" in processed.issue
            if processed.physical_issue:
                assert "SECRET_PASSWORD" not in processed.physical_issue
                assert "SECRET_TOKEN" not in processed.physical_issue
        status_logs = [
            record for record in caplog.records if record.message.startswith("Settlement ")
        ]
        assert status_logs
        assert all(".." not in record.message for record in status_logs)
        assert all(
            record.levelno == (logging.WARNING if runtime.attention else logging.INFO)
            for record in status_logs
        )
        assert "SECRET_PASSWORD" not in caplog.text
        assert "SECRET_TOKEN" not in caplog.text
        assert "private.invalid" not in caplog.text
        assert not documents.exists("head.json")
        assert store.results(documents.read("working.json")["revision"], START, END) == []
        assert repository.get_plan(document["plan_id"]) == document
    finally:
        repository.close()


def test_statistics_checkpoint_recovery_publishes_new_version_and_retains_previous_results(
    tmp_path,
):
    """A 1-kWh import costs .30 versus .20 reference depletion, retaining -.10 savings across historical recovery."""
    from dataclasses import replace

    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.contracts.bonus_statistics import (
        StatisticReference,
        StatisticsBasis,
        StatisticsChannel,
    )

    repository = SqlPlanRepository(tmp_path)
    selected_contract = contract(bonus=True)
    period = selected_contract.bonuses[0].period_start(START)
    original_plan = {
        "plan_id": "historical-recovery-original",
        "generated_at": START.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "steps": [],
        "input_snapshot": {
            "contract_version": selected_contract.version,
            "problem": json.loads(
                json.dumps(asdict(problem()), default=lambda value: value.isoformat())
            ),
        },
    }
    repository.state["plan"] = original_plan
    repository.save()
    store = SettlementStore(repository.engine)
    documents = SqlDocuments(repository.engine, "savings")
    with repository.engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(START), plan_id=original_plan["plan_id"])
        )
        retain(
            connection,
            (
                PriceSeries(
                    "published",
                    "NL",
                    "EUR",
                    START - timedelta(hours=1),
                    None,
                    None,
                    (PricePoint(START, END, 0.1, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    store.put_frames((frame(),), {"bindings": bindings(), "seeds": [], "location": None})
    basis = StatisticsBasis(
        period,
        START,
        (StatisticReference("sensor.export", 1, "recorder", "kWh", 0, 1),),
        0,
        None,
        "a" * 64,
        True,
    )
    first = CounterSeed(
        "bonus", period, 1, 10, "sensor.export", START, (("sensor.export", 10),), basis
    )
    initial = CounterBook(START, (first,))
    now = START + timedelta(days=5)
    later = replace(
        first,
        used_kwh=2,
        meter_kwh=20,
        observed_at=now,
        component_meters=(("sensor.export", 20),),
        statistics=replace(basis, latest_at=now, series=(replace(basis.series[0], latest_sum=2),)),
    )
    current = CounterBook(now, (later,))
    runtime = SavingsRuntime(store, documents, repository)
    try:
        runtime.process(selected_contract, now, initial)
        assert runtime.issue is None
        before = runtime.overview(START, END)
        assert before["coverage"]["evaluated"] == 1
        assert before["savings"] == pytest.approx(-0.1)
        original_frames = store.frames()
        runtime.process(selected_contract, now, current)
        assert runtime.issue == "Historical bonus baseline is being read from Home Assistant"
        assert runtime.overview(START, END)["revision"] == before["revision"]
        pending = runtime.bonus.pending
        channels = (
            StatisticsChannel(
                basis.series[0],
                (
                    (period - timedelta(hours=1), 0, 0),
                    (START - timedelta(hours=1), 1, 10),
                ),
            ),
        )
        runtime.bonus.receive_statistics(pending, channels, current, now)
        runtime.process(selected_contract, now, current)
        assert runtime.issue is None
        after = runtime.overview(START, END)
        assert after["revision"] != before["revision"]
        assert after["savings"] == pytest.approx(-0.1)
        assert runtime.overview(START, END, before["revision"])["savings"] == pytest.approx(-0.1)
        assert store.frames() == original_frames
        assert repository.get_plan(original_plan["plan_id"]) == original_plan
        assert current.seeds == (later,)
    finally:
        repository.close()
