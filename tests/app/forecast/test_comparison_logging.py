"""Report comparison availability from accepted store state, never a successful no-op alone."""

import asyncio
import logging
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from open_energy_planner.forecast_selection import api
from open_energy_planner.forecast_selection.policy import SELECTION_POLICY
from open_energy_planner.forecast_selection.runtime import ModelSelection
from open_energy_planner.forecast_selection.storage import SELECTION_INDEX, SelectionStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository


def test_comparison_worker_waits_for_readable_storage_before_reporting_recovery(
    tmp_path, monkeypatch, caplog
):
    """A real unreadable selection archive cannot report readiness. Repair enables one recovery event."""
    repository = SqlPlanRepository(tmp_path)
    documents = SqlDocuments(repository.engine, "planning")
    documents.write(SELECTION_INDEX, {"policy": "SECRET_UNSUPPORTED_POLICY", "families": {}})
    models = []
    initialize = ModelSelection.__init__

    def remember_model(self, *arguments):
        """Observe the actual model and store without replacing their processing behavior."""
        initialize(self, *arguments)
        models.append(self)

    monkeypatch.setattr(ModelSelection, "__init__", remember_model)
    monkeypatch.setattr(api, "MODEL_WORKER_POLL_SECONDS", 0.01)
    choice = {
        "family_id": "comparison-family",
        "scope": "demand",
        "kind": "demand",
        "label": "Household consumption",
        "eligibility_issue": None,
        "baseline": None,
        "candidate": None,
    }
    monkeypatch.setattr(api, "variants", lambda *_arguments: [choice])
    installation = SimpleNamespace(
        current=SimpleNamespace(
            inverters=(), batteries=(), import_limit_w=1000, export_limit_w=1000
        ),
        issue=None,
    )
    preferences = []
    demand = SimpleNamespace(
        configuration=SimpleNamespace(version="demand"),
        store=SimpleNamespace(issue=None),
        select_learned=preferences.append,
    )
    solar = SimpleNamespace(
        configuration=SimpleNamespace(version="solar", inverters=(), sources=()),
        store=SimpleNamespace(issue=None),
    )
    repository.state["plan"] = {
        "strategy": "minimize cost",
        "currency": "EUR",
        "input_snapshot": {
            "contract_version": "contract",
            "problem": {"grid_import_limit_w": 1000, "grid_export_limit_w": 1000, "inverters": []},
        },
        "comparison_bindings": {"batteries": []},
    }

    async def scenario():
        """Keep the same worker alive through failed and repaired actual SQL archive reads."""
        app = web.Application()
        api.register(
            app,
            repository,
            None,
            SimpleNamespace(current=SimpleNamespace(version="contract"), issue=None),
            SimpleNamespace(current=None, issue=None),
            installation,
            demand,
            solar,
            SimpleNamespace(current="minimize cost", issue=None),
            asyncio.Lock(),
            enabled=True,
        )
        async with TestServer(app):
            async with asyncio.timeout(3):
                while not any(record.levelno == logging.DEBUG for record in caplog.records):
                    await asyncio.sleep(0.005)
                assert models[0].store.issue == "model_selection_archive_unreadable"
                assert not any(
                    "ready" in message or "recovered" in message for message in caplog.messages
                )
                documents.write(SELECTION_INDEX, {"policy": SELECTION_POLICY, "families": {}})
                models[0].store = SelectionStore(documents)
                while not any("recovered" in message for message in caplog.messages):
                    await asyncio.sleep(0.005)
                assert models[0].issue is None

    try:
        with caplog.at_level(logging.DEBUG, logger=api.__name__):
            asyncio.run(scenario())
        assert any(record.levelno == logging.WARNING for record in caplog.records)
        recoveries = [record for record in caplog.records if "recovered" in record.message]
        assert len(recoveries) == 1 and recoveries[0].levelno == logging.INFO
        assert "SECRET" not in caplog.text
        assert preferences and not any(preferences)
    finally:
        repository.close()
