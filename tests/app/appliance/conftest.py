"""Share real SQLite appliance-learning storage across focused behavior tests."""

import pytest

from open_energy_planner.appliances.cycles.service import ApplianceService
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.appliance.test_appliance_learning import ENTITY, POWER


@pytest.fixture
def service(tmp_path):
    """Persist profiles in the real shared SQLite document store and close it afterward."""
    repository = SqlPlanRepository(tmp_path)
    consumers = ConsumerForecasts(SqlDocuments(repository.engine, "demand"))
    consumers.sync(
        {
            "entities": [{"entity_id": ENTITY, "name": "Washer"}],
            "suggestions": {
                "consumers": [{"entity_id": ENTITY, "upstream": None, "power_entity": POWER}]
            },
        }
    )
    consumers.save([{"entity_id": ENTITY, "weather_sensitive": False}], consumers.version)
    result = ApplianceService(SqlDocuments(repository.engine, "planning"), consumers)
    yield result
    repository.close()
