"""One real-backend EV service fixture without implicit charging requests."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from open_energy_planner.appliances.ev.service import EVService
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.app.ev.support.devices import catalogue, settings
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[name for name, _url in BACKENDS])
def ev_service(request):
    """Use the shared four-backend matrix while keeping each test's schema isolated."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(catalogue())
    service = EVService(
        SqlDocuments(engine, "planning"), consumers, SimpleNamespace(state={"devices": {}})
    )
    service.configure(settings(), catalogue())
    yield service
    SCHEMA.drop_all(engine)
    engine.dispose()
