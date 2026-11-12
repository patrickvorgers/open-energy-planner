"""Independent two-quarter browser fixture: actual .60 cost versus .40 reference inventory consumption."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from open_energy_planner.build_info import RELEASE
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import ContractPeriod, ContractSchedule, TariffEvidence
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.savings.model import Frame
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository


def seed(directory, *, corrected=False, now=None):
    """Store honest simulated expectations and different realized meters, without mocks or real hardware."""
    now = datetime.now(UTC) if now is None else now
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    steps = tuple(
        Interval(
            start + timedelta(minutes=index * 15), 0.25, 0.3, 0, 1, 0, pv_sources=(("roof", 0),)
        )
        for index in range(2)
    )
    problem = InstallationProblem(
        steps,
        (SolarInverter("inverter", 4000, 1, True),),
        (BatteryState("battery", "inverter", Battery(4, 2, 0, 4000, 4000)),),
        (PVUnit("roof", "inverter", False),),
        10000,
        10000,
        terminal_valuations=(
            TerminalValuation(
                "battery", 0, 4, 0.2, start, start + timedelta(hours=24), "trailing_window_proxy"
            ),
        ),
    )
    schedule = ContractSchedule(
        "browser",
        "NL",
        "EUR",
        start - timedelta(days=1),
        (
            ContractPeriod(
                start - timedelta(days=1),
                None,
                Formula("0.4" if corrected else "0.3"),
                Formula("0"),
            ),
        ),
    )
    plan = {
        "schema": 2,
        "release": RELEASE,
        "plan_id": "browser-original-plan",
        "config_revision": "browser-config",
        "generated_at": start.isoformat(),
        "valid_until": (start + timedelta(minutes=30)).isoformat(),
        "currency": "EUR",
        "market_area": "NL",
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "comparison_names": {"battery": "Original battery", "roof": "Original roof"},
        "comparison_bindings": {
            "batteries": [{"device_id": "battery", "energy_entity": "sensor.discharge"}],
            "solar": [{"source_id": "roof", "entity_id": "sensor.pv"}],
        },
        "input_snapshot": {
            "contract_version": schedule.version if not corrected else "original-browser-contract",
            "tariff_evidence": json.loads(
                json.dumps(
                    [
                        asdict(
                            TariffEvidence(
                                step.start,
                                step.start + timedelta(minutes=15),
                                0.1,
                                0.3,
                                0,
                                "published",
                                15,
                                "test-market",
                                "original-price",
                                "original-browser-contract",
                                0,
                            )
                        )
                        for step in steps
                    ],
                    default=lambda value_item: value_item.isoformat(),
                )
            ),
            "problem": json.loads(
                json.dumps(asdict(problem), default=lambda value: value.isoformat())
            ),
        },
        "expected_flows": [
            {
                "grid_import": 0,
                "grid_export": 0,
                "inverters": [
                    {"inverter_id": "inverter", "import_kwh": 0, "export_kwh": 1, "clipping": False}
                ],
                "solar": [{"source_id": "roof", "used_kwh": 0, "enabled": True}],
                "batteries": [
                    {
                        "device_id": "battery",
                        "charge_kwh": 0,
                        "discharge_kwh": 1,
                        "end_kwh": 1 - index,
                    }
                ],
            }
            for index in range(2)
        ],
        "independent_costs": {
            "import_cost": 0,
            "export_income": 0,
            "bonus_income": 0,
            "wear_cost": 0,
            "cash_cost": 0,
            "terminal_value": 0,
            "objective": 0,
        },
        "steps": [
            {
                "start": step.start.isoformat(),
                "end": (step.start + timedelta(hours=step.duration_h)).isoformat(),
                "devices": {
                    "battery": {"mode": "fixed_power", "power_w": 4000},
                    "roof": {"enabled": True},
                },
                "grid_active": False,
            }
            for step in steps
        ],
    }
    plans = SqlPlanRepository(directory)
    try:
        plans.state["plan"] = plan
        plans.save()
        with plans.engine.begin() as connection:
            connection.execute(
                ACTIVATIONS.insert().values(activated_at=stamp(start), plan_id=plan["plan_id"])
            )
        ContractStore(SqlDocuments(plans.engine, "contracts")).save(schedule)
        PriceStore(SqlDocuments(plans.engine, "prices")).save(
            (
                PriceSeries(
                    "test-published",
                    "NL",
                    "EUR",
                    start,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            step.start,
                            step.start + timedelta(minutes=15),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for step in steps
                    ),
                ),
            )
        )
        context = {
            "bindings": {
                "terms": [
                    {"entity_id": "sensor.import", "sign": 1},
                    {"entity_id": "sensor.pv", "sign": 1},
                    {"entity_id": "sensor.discharge", "sign": 1},
                ],
                "imports": ["sensor.import"],
                "exports": [],
                "solar": [{"source_id": "roof", "entity_id": "sensor.pv"}],
                "batteries": [{"device_id": "battery", "energy_entity": "sensor.discharge"}],
            },
            "seeds": [],
            "location": None,
        }
        store = SettlementStore(plans.engine)
        store.put_frames(
            tuple(
                Frame(
                    step.start,
                    step.start + timedelta(minutes=15),
                    {"sensor.import": 1, "sensor.pv": 0, "sensor.discharge": 0},
                    {"battery": 2},
                    {"battery": 2},
                    {},
                    "recorded",
                )
                for step in steps
            ),
            context,
        )
        SavingsRuntime(store, SqlDocuments(plans.engine, "savings"), plans).process(schedule, now)
        if corrected:
            from open_energy_planner.savings.repricing import PlanValuations

            PlanValuations(SqlDocuments(plans.engine, "savings"), plans).process(
                schedule, None, now
            )
    finally:
        plans.close()
