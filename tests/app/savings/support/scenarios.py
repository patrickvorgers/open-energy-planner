"""Independent physical frames, contracts and reference problems shared by savings scenarios."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.contracts.bonus import BonusRule
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import ContractPeriod, ContractSchedule
from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.savings.model import Frame
from open_energy_planner.solar.calculation.model import SolarInverter

START = datetime(2026, 10, 22, 10, tzinfo=UTC)

END = START + timedelta(minutes=15)


def problem(*, pv_kwh=0, load_kwh=1, hybrid=True, efficiency=1, cycle_fee=0):
    """One 4-kWh battery, a 4-kW converter and a quarter. All numerical expectations are independent."""
    return InstallationProblem(
        (Interval(START, 0.25, 0.3, -0.1, load_kwh, pv_kwh, pv_sources=(("roof", pv_kwh),)),),
        (SolarInverter("inv", 4000, efficiency, hybrid),),
        (BatteryState("battery", "inv", Battery(4, 2, 0, 4000, 4000, 1, 1, cycle_fee)),),
        (PVUnit("roof", "inv", False),),
        10000,
        10000,
        terminal_valuations=(
            TerminalValuation(
                "battery", 0, 4, 0.2, START, START + timedelta(hours=24), "trailing_window_proxy"
            ),
        ),
    )


def contract(*, bonus=False, buy="0.3", sell="-0.1"):
    """Fixed negative export rates ensure ordinary reference export remains costly."""
    rules = (
        (BonusRule("bonus", START - timedelta(days=1), Formula("0.1"), 2, timezone="UTC"),)
        if bonus
        else ()
    )
    return ContractSchedule(
        "test",
        "NL",
        "EUR",
        START - timedelta(days=1),
        (ContractPeriod(START - timedelta(days=2), None, Formula(buy), Formula(sell)),),
        rules,
    )


def frame(
    *,
    start=START,
    imported_kwh=1,
    exported_kwh=0,
    pv_kwh=0,
    discharged_kwh=0,
    battery_start_kwh=2,
    battery_end_kwh=2,
):
    """Measured channel balance is import plus solar/discharge minus export. Battery endpoints are explicit."""
    return Frame(
        start,
        start + timedelta(minutes=15),
        {
            "sensor.import": imported_kwh,
            "sensor.export": exported_kwh,
            "sensor.pv": pv_kwh,
            "sensor.discharge": discharged_kwh,
        },
        {"battery": battery_start_kwh},
        {"battery": battery_end_kwh},
        {"sensor.export": 10},
        "recorded",
    )


def bindings():
    """Use the same four disjoint AC channels for household balance and grid accounting."""
    return {
        "terms": [
            {"entity_id": entity, "sign": sign}
            for entity, sign in (
                ("sensor.import", 1),
                ("sensor.export", -1),
                ("sensor.pv", 1),
                ("sensor.discharge", 1),
            )
        ],
        "imports": ["sensor.import"],
        "exports": ["sensor.export"],
        "solar": [{"source_id": "roof", "entity_id": "sensor.pv"}],
        "batteries": [{"device_id": "battery", "energy_entity": "sensor.discharge"}],
    }


def prices(start=START):
    """Published wholesale evidence covers precisely one tested quarter."""
    return [
        {
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=15)).isoformat(),
            "price": -0.1,
            "version": "published-source-v1",
        }
    ]
