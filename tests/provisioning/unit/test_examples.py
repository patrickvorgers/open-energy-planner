"""Ordinary example automation identities without lifecycle acceptance."""

import pytest

from script.provisioning import device_examples as examples
from script.provisioning.automations import automations
from tests.provisioning.support.registry import example_registry


def test_ambiguous_virtual_identities_are_rejected(tmp_path):
    """Two registry rows for the same technical point never select arbitrary equipment."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    registry, states = example_registry(journal)
    duplicate = {
        **next(
            row
            for row in registry
            if row["entity_id"] == "binary_sensor.renamed_wallbox_a_charging"
        ),
        "entity_id": "binary_sensor.duplicate",
    }
    registry.append(duplicate)
    with pytest.raises(LookupError, match="unique virtual"):
        automations(registry, states, journal)
