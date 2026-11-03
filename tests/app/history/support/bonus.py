"""Independent measured history and UTC annual bonus inputs, separate from test scenarios."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.contracts.wire import parse_contract
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.home_assistant.observation_wire import parse_configuration
from tests.app.contract.support.settings import settings

NOW = datetime(2026, 1, 1, 5, 30, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)


def history():
    """Create an anchor and four hours with 2 kWh each, including a physical meter reset."""
    return EnergyHistory(
        "sensor.export",
        tuple(
            EnergyHour(START + timedelta(hours=index_item - 1), 100 + index_item * 2, meter)
            for index_item, meter in enumerate((500, 502, 1, 3, 5))
        ),
    )


def inputs():
    """Configure a UTC calendar-year bonus and required aggregate export source."""
    body = settings()
    body["bonuses"] = [
        {
            "rule_id": "solar",
            "effective_from": START.isoformat(),
            "rate_formula": "0.04",
            "cap_kwh": 100,
            "timezone": "UTC",
        }
    ]
    contract = parse_contract(body, NOW)
    configuration = parse_configuration(
        {
            "bindings": [
                {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                {
                    "key": "grid.export_energy",
                    "entity_id": "sensor.export",
                    "quantity": "energy",
                },
            ]
        },
        revision="config-1",
    )
    return contract, configuration
