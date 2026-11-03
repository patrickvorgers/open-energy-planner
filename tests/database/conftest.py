"""Shared real-backend fixtures for selection contracts and retained-history performance proofs."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from tests.database.support.backends import BACKENDS
from tests.database.support.settlement import settlement

__all__ = ["settlement"]


@pytest.fixture(params=BACKENDS, ids=[item[0] for item in BACKENDS])
def selection_store(request, tmp_path):
    """Use only dedicated test databases, with a unique area per case and no destructive cleanup SQL."""
    name, url = request.param
    adapter = (
        SqlAlchemySelectionStore.local(tmp_path)
        if name == "sqlite"
        else SqlAlchemySelectionStore(create_engine(url, pool_pre_ping=True))
    )
    yield adapter, "T" + uuid4().hex[:20]
    adapter.engine.dispose()
