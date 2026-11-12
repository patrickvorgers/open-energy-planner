"""Durable measured calibration across every supported active database backend."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from open_energy_planner.installation.model import Installation, InstalledBattery
from open_energy_planner.installation.storage import InstallationStore
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def documents(request):
    """Use an isolated real schema for restart, rollback and bounded-ledger checks."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    yield SqlDocuments(engine, "installation")
    SCHEMA.drop_all(engine)
    engine.dispose()


def test_configure_restart_and_failed_write_preserve_calibration(documents, monkeypatch):
    """An enabled policy survives reopen. A failed replacement must preserve its prior settings and progress."""
    battery = InstalledBattery(
        "battery", "inv", "sensor.soc", "sensor.power", 2, 4000, 4000, calibration_enabled=True
    )
    settings = Installation("sensor.grid", 4000, 4000, (SolarInverter("inv", 4000, 1),), (battery,))
    store = InstallationStore(documents)
    store.save(settings)
    original = store.calibration.state.copy()
    restored = InstallationStore(documents)
    assert restored.current == settings
    assert restored.calibration.state == original
    saved = documents.write

    def refuse_ledger(name, value):
        """Fail after immutable settings are written but before the transaction can commit."""
        if name == "battery-calibration.json":
            raise OSError("Synthetic storage failure")
        return saved(name, value)

    monkeypatch.setattr(documents, "write", refuse_ledger)
    with pytest.raises(OSError):
        store.save(replace(settings, batteries=(replace(battery, calibration_hold_hours=4),)))
    assert store.current == settings
    assert store.calibration.state == original
    reopened = InstallationStore(documents)
    assert reopened.current == settings
    assert reopened.calibration.state == original


def test_completed_hold_survives_restart_and_unrelated_edit(documents):
    """Real observations count once, remain independent of an immutable configuration hash and survive a grid edit."""
    battery = InstalledBattery(
        "battery",
        "inv",
        "sensor.soc",
        "sensor.power",
        2,
        4000,
        4000,
        calibration_enabled=True,
        calibration_hold_hours=0.25,
    )
    settings = Installation("sensor.grid", 4000, 4000, (SolarInverter("inv", 4000, 1),), (battery,))
    store = InstallationStore(documents)
    store.save(settings)
    start = datetime.now(UTC)
    for minute in (0, 5, 10, 15):
        report = SimpleNamespace(
            captured_at=start + timedelta(minutes=minute),
            readings=tuple(
                SimpleNamespace(
                    key=f"battery.battery.{key}",
                    entity_id=entity,
                    value=value,
                    unit=unit,
                    timestamp_kind="ha_state",
                    observed_at=start + timedelta(minutes=minute),
                )
                for key, entity, value, unit in (
                    ("soc", "sensor.soc", 100 if minute == 0 else 98, "%"),
                    ("power", "sensor.power", 0, "W"),
                )
            ),
        )
        store.observe(report)
    assert store.current.version == settings.version
    completed = (start + timedelta(minutes=15)).isoformat()
    assert store.calibration.state["battery"]["completed_at"] == completed
    store.save(replace(settings, import_limit_w=3000))
    restored = InstallationStore(documents)
    assert restored.calibration.state["battery"]["completed_at"] == completed
    assert restored.calibration.due_at(battery) == start + timedelta(minutes=15, days=14)


def test_missing_progress_requires_explicit_disable_before_reset(documents):
    """A missing enabled-battery row is a visible fault. Saving unrelated settings must not silently invent a fresh deadline."""
    battery = InstalledBattery(
        "battery", "inv", "sensor.soc", "sensor.power", 2, 4000, 4000, calibration_enabled=True
    )
    settings = Installation("sensor.grid", 4000, 4000, (SolarInverter("inv", 4000, 1),), (battery,))
    store = InstallationStore(documents)
    store.save(settings)
    documents.write("battery-calibration.json", {"schema": 1, "batteries": {}})
    restored = InstallationStore(documents)
    assert restored.current == settings
    assert restored.calibration.issue == "battery_calibration_unreadable"
    with pytest.raises(OSError):
        restored.save(replace(settings, import_limit_w=3000))
    restored.save(replace(settings, batteries=(replace(battery, calibration_enabled=False),)))
    assert restored.calibration.issue is None
    restored.save(settings)
    assert restored.calibration.state["battery"]["completed_at"] is None
