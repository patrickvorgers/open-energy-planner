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

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings import api as savings_api
from open_energy_planner.savings.bonus import HistoricalBonuses
from open_energy_planner.savings.model import PendingSettlement
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS, PUBLISHED_PRICES
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame, problem

PRIVATE_EVIDENCE = (
    "postgresql://private-user:SECRET_PASSWORD@private.invalid/archive?token=SECRET_TOKEN"
)


@pytest.mark.parametrize(
    "fault", ["plan_timestamp", "price_timestamp", "pending", "missing_prices"]
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
    if fault == "pending":

        def await_private_evidence(_runtime, *_arguments):
            """Model unrecognized pending evidence text without declaring it safe for public status."""
            raise PendingSettlement(PRIVATE_EVIDENCE)

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
            lambda: None,
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
            assert processed.attention is (fault in {"plan_timestamp", "price_timestamp"})
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
