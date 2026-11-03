"""Disposable real-backend settlement setup shared by functional and volume tests."""

import json
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS, SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.app.savings.support.scenarios import END, START, bindings, frame, problem
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[item[0] for item in BACKENDS])
def settlement(request, tmp_path):
    """Seed one original plan, activation, two 0.1 prices and two settlement frames.

    Use the selected BACKENDS URL or a local SQLite archive engine. Yield
    (runtime, store, docs, engine, plans), then drop the schema and dispose the
    engine even when setup fails. Only dedicated disposable databases are used.
    """
    _name, url = request.param
    engine = create_engine(url) if url else archive_engine(tmp_path)
    try:
        SCHEMA.drop_all(engine)
        plans = SqlPlanRepository(tmp_path, engine=engine)
        store = SettlementStore(engine)
        docs = SqlDocuments(engine, "savings")
        runtime = SavingsRuntime(store, docs, plans)
        document = {
            "plan_id": "original-plan",
            "generated_at": START.isoformat(),
            "strategy": "minimize cost",
            "solution_status": "optimal",
            "input_snapshot": {
                "problem": json.loads(
                    json.dumps(asdict(problem()), default=lambda value: value.isoformat())
                )
            },
            "steps": [],
        }
        plans.state["plan"] = document
        plans.save()
        with engine.begin() as connection:
            connection.execute(
                ACTIVATIONS.insert().values(activated_at=stamp(START), plan_id=document["plan_id"])
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
                        tuple(
                            PricePoint(
                                START + timedelta(minutes=index * 15),
                                START + timedelta(minutes=(index + 1) * 15),
                                0.1,
                                PriceKind.PUBLISHED,
                                15,
                            )
                            for index in range(2)
                        ),
                    ),
                ),
            )
        context = {"bindings": bindings(), "seeds": [], "location": None}
        store.put_frames((frame(), frame(start=END)), context)
        yield runtime, store, docs, engine, plans
    finally:
        try:
            SCHEMA.drop_all(engine)
        finally:
            engine.dispose()
