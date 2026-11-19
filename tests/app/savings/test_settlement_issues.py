"""Settlement failures expose bounded evidence guidance while retaining raw archives and status semantics."""

import asyncio
import logging
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from open_energy_planner.savings import api as savings_api
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

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
