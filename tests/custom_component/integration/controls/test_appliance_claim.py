"""Exercise real HA service responses and blueprint admission before simulated physical starts."""

import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant", reason="Run in the pinned real-HA test image")

from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.appliances import consumer_identity
from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import running_chain


@pytest.mark.parametrize(
    "reply_case",
    [
        "fresh",
        "expired",
        "boundary",
        "missing_expiry",
        "invalid_expiry",
        "denied",
        "string_authorization",
        "wrong_task",
        "wrong_attempt",
        "sync_expired",
        "delivery_expired",
    ],
)
def test_claim_and_blueprint_reject_unusable_or_late_responses(monkeypatch, reply_case):
    """Only a matching, strictly authorized future lease may reach the simulated start action.

    Controlled RPC responses isolate receiver admission from app-side task state.
    Real HA executes the bundled blueprint and the actual integration service.
    The delivery case expires after the service returns, so the blueprint must
    independently check the deadline immediately before invoking physical work.
    """

    async def scenario():
        """Run one isolated HA lifecycle with controlled claim timing and no physical equipment."""
        async with running_chain(fixture_devices=False) as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            entity = "sensor.claim_test_dishwasher"
            coordinator.consumer_forecasts = {"options": [{"entity_id": entity}]}
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, entry.entry_id + "_" + consumer_identity(entity))},
                name="Claim test dishwasher",
            )
            starts, requests = [], []

            async def physical_start(_call):
                """Count executed physical actions independently of the claim response contents."""
                starts.append("started")

            hass.services.async_register("appliance_fixture", "start", physical_start)
            assert await async_setup_component(
                hass,
                "automation",
                {
                    "automation": [
                        {
                            "alias": "Claim lease acceptance",
                            "use_blueprint": {
                                "path": "open_energy_planner/appliance.yaml",
                                "input": {
                                    "appliance": device.id,
                                    "start_action": [{"action": "appliance_fixture.start"}],
                                    "started_trigger": [
                                        {"trigger": "event", "event_type": "fixture_started"}
                                    ],
                                    "completed_trigger": [
                                        {"trigger": "event", "event_type": "fixture_completed"}
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
            await hass.async_block_till_done()
            clock = [dt_util.utcnow()]
            response = {
                "authorized": True,
                "task_id": "task",
                "attempt_id": "attempt",
                "expires_at": (clock[0] + timedelta(seconds=60)).isoformat(),
            }
            if reply_case == "expired":
                response["expires_at"] = (clock[0] - timedelta(seconds=1)).isoformat()
            elif reply_case == "boundary":
                response["expires_at"] = clock[0].isoformat()
            elif reply_case == "missing_expiry":
                response.pop("expires_at")
            elif reply_case == "invalid_expiry":
                response["expires_at"] = "not-a-timestamp"
            elif reply_case == "denied":
                response["authorized"] = False
            elif reply_case == "string_authorization":
                response["authorized"] = "true"
            elif reply_case == "wrong_task":
                response["task_id"] = "other-task"
            elif reply_case == "wrong_attempt":
                response["attempt_id"] = "other-attempt"
            original_request = coordinator.client.request

            async def claim_response(method, path, body=None):
                """Expose a controlled app lease while other client requests keep their normal path."""
                if method == "POST" and path == "/v1/appliances/claim":
                    requests.append(body)
                    return {"result": deepcopy(response)}
                return await original_request(method, path, body)

            async def sync():
                """Keep the controlled projection and optionally consume the lease while syncing."""
                if reply_case == "sync_expired":
                    clock[0] += timedelta(seconds=120)

            registry_type = type(hass.services)
            original_call = registry_type.async_call

            async def deliver(registry, domain, service, *arguments, **keywords):
                """Simulate result-delivery latency after the actual claim handler has finished."""
                result = await original_call(registry, domain, service, *arguments, **keywords)
                if (
                    domain == DOMAIN
                    and service == "appliance_claim"
                    and reply_case == "delivery_expired"
                ):
                    clock[0] += timedelta(seconds=120)
                return result

            monkeypatch.setattr(coordinator.client, "request", claim_response)
            monkeypatch.setattr(coordinator, "sync", sync)
            # Templates use now(), while protocol admission uses utcnow().
            monkeypatch.setattr(dt_util, "utcnow", lambda: clock[0])
            monkeypatch.setattr(
                dt_util,
                "now",
                lambda time_zone=None: clock[0].astimezone(time_zone or dt_util.DEFAULT_TIME_ZONE),
            )
            monkeypatch.setattr(registry_type, "async_call", deliver)
            hass.bus.async_fire(
                DOMAIN + "_appliance_request",
                {"device_id": device.id, "task_id": "task", "attempt_id": "attempt"},
            )
            await hass.async_block_till_done()
            assert requests == [{"entity_id": entity, "task_id": "task", "attempt_id": "attempt"}]
            assert starts == (["started"] if reply_case == "fresh" else [])

    asyncio.run(scenario())
