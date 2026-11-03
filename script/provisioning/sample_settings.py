"""Prepare only app-side sample contract settings after the isolated HA fixture becomes ready."""

import asyncio

from aiohttp import ClientSession

from script.provisioning.device_examples import zonneplan_contract


async def provision(*, app_url, token):
    """Wait for real integration-uploaded Energy/history metadata and seed one synthetic contract idempotently."""
    async with ClientSession(headers={"Authorization": "Bearer " + token}) as session:
        async with session.get(app_url + "/v1/setup?refresh=1") as response:
            response.raise_for_status()
        for _ in range(60):
            async with session.get(app_url + "/v1/setup") as response:
                response.raise_for_status()
                setup = await response.json()
            data = setup["catalogue"]["data"]
            if (
                data
                and data["suggestions"]["energy_status"] == "ready"
                and setup["annual_estimate"]
            ):
                if any(
                    not energy_term["entity_id"].startswith("sensor.oep_test_")
                    for energy_term in data["suggestions"]["terms"]
                ):
                    raise ValueError("Refusing an unexpected non-laboratory installation")
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError(
                "Test Energy dashboard/history not ready. Inspect the fixture integration log"
            )
        async with session.get(app_url + "/v1/contracts") as response:
            contract = await response.json()
        if contract["status"] == "not_configured":
            body = zonneplan_contract(data["timezone"])
            async with session.put(app_url + "/v1/contracts", json=body) as response:
                response.raise_for_status()
        if all(setup[key] is None for key in ("installation", "solar", "demand")):
            roof = {"inverter_id": "roof", "ac_limit_w": 3000, "efficiency": 0.96, "hybrid": True}
            garage = {
                "inverter_id": "garage",
                "ac_limit_w": 1500,
                "efficiency": 0.96,
                "hybrid": False,
            }
            body = {
                "installation": {
                    "grid_power_entity": "sensor.oep_test_grid_power",
                    "grid_export_entity": "sensor.oep_test_grid_export",
                    "import_limit_w": 11000,
                    "export_limit_w": 11000,
                    "inverters": [roof],
                    "batteries": [
                        {
                            "device_id": "lab_battery",
                            "energy_entity": "sensor.oep_test_battery_discharge",
                            "charge_energy_entity": "sensor.oep_test_battery_charge",
                            "inverter_id": "roof",
                            "soc_entity": "sensor.oep_test_battery_soc",
                            "power_entity": "sensor.oep_test_battery_power",
                            "capacity_kwh": 10,
                            "charge_limit_w": 3000,
                            "discharge_limit_w": 3000,
                            "minimum_soc": 10,
                        }
                    ],
                },
                "solar": {
                    "sources": [
                        {
                            "source_id": "lab_roof",
                            "entity_id": "sensor.oep_test_solar_roof",
                            "inverter_id": "roof",
                            "capacity_w": 3000,
                            "orientation": "south",
                            "can_curtail": True,
                        },
                        {
                            "source_id": "lab_garage",
                            "entity_id": "sensor.oep_test_solar_garage",
                            "inverter_id": "garage",
                            "capacity_w": 1500,
                            "orientation": "west",
                            "can_curtail": True,
                        },
                    ],
                    "inverters": [roof, garage],
                },
                "demand": {
                    "annual_kwh": setup["annual_estimate"]["annual_kwh"],
                    "timezone": data["timezone"],
                    "terms": data["suggestions"]["terms"],
                    "exclusions": [],
                },
            }
            async with session.post(app_url + "/v1/setup/preview", json=body) as response:
                response.raise_for_status()
            for key in ("demand", "solar", "installation"):
                async with session.put(app_url + "/v1/" + key, json=body[key]) as response:
                    response.raise_for_status()
        elif any(setup[key] is None for key in ("installation", "solar", "demand")):
            print(
                "Partial setup preserved. Complete the missing sections in the app wizard. "
                "Sample device defaults were not combined with your saved settings."
            )
            return
        print(
            f"Test Energy ready: {setup['annual_estimate']['days']} complete days. "
            "Installation settings preserved or initialized. Open the app wizard to review or edit."
        )
