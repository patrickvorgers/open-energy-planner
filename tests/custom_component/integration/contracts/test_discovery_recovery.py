"""Keep optional source-binding faults separate from valid command synchronization."""

import asyncio
from datetime import timedelta

from tests.custom_component.support.chain import running_chain


def test_rejected_bindings_do_not_block_ack_or_confirm_old_metadata(monkeypatch, caplog):
    """Reject unsupported discovery bindings without blocking plans or confirming old membership."""

    async def scenario():
        """Retain actual plan/ack traffic while supplying a rejected 128-character source identity."""
        async with running_chain() as (_hass, entry, *_):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            original = coordinator.client.request
            bindings, metadata, acknowledgements, forecasts = [], [], [], []

            async def request(method, path, body=None):
                """Keep genuine plan validation and acknowledgements independent from optional discovery."""
                if path == "/v1/ha/catalogue":
                    metadata.append(body)
                    return {"accepted": True}
                if path == "/v1/solar/ha-forecasts":
                    forecasts.append(body)
                    return {"accepted": True}
                if path == "/v1/ack":
                    acknowledgements.append(body)
                data = await original(method, path, body)
                if path == "/v1/snapshot":
                    data["source"] = "home_assistant"
                    data["installation"]["setup_source_entities"] = bindings.copy()
                    data["acknowledged_plan"] = None
                return data

            monkeypatch.setattr(coordinator.client, "request", request)
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            accepted = coordinator.catalogue_context
            before = (len(metadata), len(forecasts), len(acknowledgements))
            rejected = "sensor." + "x" * 121
            assert len(rejected) == 128
            bindings.append(rejected)
            coordinator.catalogue_confirmed_at -= timedelta(minutes=2)
            for _attempt in range(3):
                await coordinator.sync()
                assert coordinator.projection.status == "active"
                assert coordinator.projection.command is not None
            assert len(acknowledgements) == before[2] + 3
            assert (len(metadata), len(forecasts)) == before[:2]
            assert coordinator.catalogue_context is accepted
            assert coordinator.catalogue_discovery._configured == set()
            assert not coordinator.catalogue_discovery.healthy
            assert rejected not in caplog.text
            warnings = [
                record
                for record in caplog.records
                if record.levelname == "WARNING" and "source bindings" in record.getMessage()
            ]
            assert len(warnings) == 1
            bindings.clear()
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            assert len(metadata) == before[0] + 1
            assert "confirmed_revision" not in metadata[-1]
            assert len(acknowledgements) == before[2] + 4

    asyncio.run(scenario())
