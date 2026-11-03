"""Audit and explicitly repair missing simulated device energy through Recorder."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import voluptuous as vol
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.core import SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .calculation.recorder.balance import ENERGY_TOLERANCE_KWH, FINE_SECONDS, repair_documents

AUDIT_DAYS = 32
SERVICE = "repair_energy_balance"
REPAIR_BOUNDARY_MARGIN_SECONDS = 20
SOURCE_OWNERSHIP_KEY = "oep_test_fixture.balance_sources"


def declare_sources(hass, entry, roles, sample):
    """Bind repair authority and the mode's canonical quarter-kWh sampler to this fixture entry."""
    owners = hass.data.setdefault(SOURCE_OWNERSHIP_KEY, {})
    owners[entry.entry_id] = {"roles": dict(roles), "sample": sample}
    entry.async_on_unload(lambda: owners.pop(entry.entry_id, None))


async def setup_balance(hass, entry):
    """Expose a lab-only preview/apply action with a durable no-replay journal."""
    from . import DOMAIN
    from .dishwasher import DATA_KEY as DISHWASHER_KEY
    from .ev import DATA_KEY as EV_KEY
    from .existing import channels

    lock = asyncio.Lock()
    store = Store(hass, 1, "oep_test_balance_repairs." + entry.entry_id)

    async def action(call):
        """Read first, require the exact preview and never replay an ambiguous queued correction."""
        async with lock:
            manager = await async_get_manager(hass)
            roles = channels((manager.data or {}).get("energy_sources", []))
            owner = hass.data.get(SOURCE_OWNERSHIP_KEY, {}).get(entry.entry_id, {})
            if owner.get("roles") != roles:
                raise ValueError(
                    "Energy sources must match this fixture's actively simulated channels"
                )
            registry = er.async_get(hass)
            consumers = [
                row["stat_consumption"]
                for row in (manager.data or {}).get("device_consumption", [])
                if (registered := registry.async_get(row["stat_consumption"]))
                and registered.platform == DOMAIN
                and registered.config_entry_id == entry.entry_id
            ]
            dishwasher = hass.data[DISHWASHER_KEY]["ids"]["energy"]
            controlled = {dishwasher, *(point["energy"] for point in hass.data[EV_KEY]["points"])}
            if not controlled <= set(consumers):
                raise ValueError("Controlled fixture meters must remain in the Energy dashboard")
            recorder = get_instance(hass)

            async def read():
                """Read both native resolutions in kWh without fabricating missing fine endpoints."""
                result = {}
                for period in ("hour", "5minute"):
                    result[period] = await recorder.async_add_executor_job(
                        statistics_during_period,
                        hass,
                        dt_util.utcnow() - timedelta(days=AUDIT_DAYS),
                        None,
                        {*roles, *consumers},
                        period,
                        {"energy": "kWh"},
                        {"sum", "state", "last_reset"},
                    )
                return result

            before = await read()
            journal = await store.async_load() or {}
            if resolution := call.data.get("resolve_ambiguous_revision"):
                if (
                    call.data.get("apply")
                    or journal.get("status") != "applying"
                    or resolution != journal.get("proposal", {}).get("revision")
                ):
                    raise ValueError(
                        "Recovery requires the exact interrupted revision and a separate call"
                    )

                def matches(expected):
                    """Accept recovery only when every journalled row has one proven outcome."""
                    if not expected:
                        return False
                    for period, entities in expected.items():
                        for entity, rows in entities.items():
                            actual = {row["start"]: row for row in before[period].get(entity, [])}
                            for row in rows:
                                current = actual.get(row["start"], {})
                                if any(
                                    abs(current.get(field, float("inf")) - row[field])
                                    > ENERGY_TOLERANCE_KWH
                                    for field in ("sum", "state")
                                    if row.get(field) is not None
                                ):
                                    return False
                    return True

                if matches(journal.get("after")):
                    status = "completed"
                elif matches(journal.get("before")):
                    status = "aborted"
                else:
                    raise ValueError(
                        "Recorder has a mixed or unverifiable outcome. No automatic recovery is permitted"
                    )
                await store.async_save({**journal, "status": status})
                return {"revision": resolution, "journal_status": status, "applied": False}
            repairs, writes = repair_documents(
                before["hour"],
                before["5minute"],
                roles,
                sorted(controlled),
                controlled,
                sample=owner["sample"],
            )
            revision = sha256(json.dumps(writes, sort_keys=True).encode()).hexdigest()
            result = {"revision": revision, "repairs": repairs, "applied": False}
            result["journal_status"] = journal.get("status")
            if call.data.get("apply") and journal.get("status") == "applying":
                raise ValueError(
                    "Previous correction outcome is ambiguous. Inspect Recorder before retrying"
                )
            if not call.data.get("apply") or not repairs:
                return result
            if call.data.get("expected_revision") != revision:
                raise ValueError("Preview changed. Inspect the current repair proposal first")
            position = dt_util.utcnow().timestamp() % FINE_SECONDS
            if min(position, FINE_SECONDS - position) < REPAIR_BOUNDARY_MARGIN_SECONDS:
                raise ValueError(
                    "Close to Recorder compilation boundary. Retry the preview after twenty seconds"
                )
            metadata = await recorder.async_add_executor_job(
                lambda: get_metadata(
                    hass, statistic_ids={entity for rows in writes.values() for entity in rows}
                )
            )
            originals = {}
            for period, entities in writes.items():
                originals[period] = {}
                for entity, rows in entities.items():
                    stamps = {row["start"] for row in rows}
                    originals[period][entity] = [
                        row for row in before[period][entity] if row["start"] in stamps
                    ]
            payloads = []
            for period, table in (("hour", Statistics), ("5minute", StatisticsShortTerm)):
                for entity, rows in writes[period].items():
                    original_metadata = metadata[entity][1]
                    unit = original_metadata["unit_of_measurement"]
                    normalized = []
                    for row in rows:
                        converted = {"start": datetime.fromtimestamp(row["start"], UTC)}
                        for field in ("sum", "state"):
                            if row.get(field) is not None:
                                converted[field] = EnergyConverter.convert(row[field], "kWh", unit)
                        if row.get("last_reset") is not None:
                            converted["last_reset"] = datetime.fromtimestamp(row["last_reset"], UTC)
                        normalized.append(converted)
                    payloads.append((original_metadata, normalized, table))
            journal = {
                "status": "applying",
                "proposal": result,
                "before": originals,
                "after": writes,
            }
            await store.async_save(journal)
            for original_metadata, normalized, table in payloads:
                recorder.async_import_statistics(original_metadata, normalized, table)
            await recorder.async_block_till_done()
            after = await read()
            remaining, _ = repair_documents(
                after["hour"],
                after["5minute"],
                roles,
                sorted(controlled),
                controlled,
                sample=owner["sample"],
            )
            if remaining:
                raise ValueError("Repair readback failed. No automatic replay is permitted")
            for period, entities in writes.items():
                for entity, rows in entities.items():
                    actual = {row["start"]: row["sum"] for row in after[period][entity]}
                    if any(
                        abs(actual.get(row["start"], float("inf")) - row["sum"])
                        > ENERGY_TOLERANCE_KWH
                        for row in rows
                    ):
                        raise ValueError("Repair readback differs from the queued replacement")
            result["applied"] = True
            result["journal_status"] = "completed"
            await store.async_save({**journal, "status": "completed", "proposal": result})
            return result

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE,
        action,
        schema=vol.Schema(
            {
                vol.Optional("apply", default=False): bool,
                vol.Optional("expected_revision"): str,
                vol.Optional("resolve_ambiguous_revision"): str,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, SERVICE))
