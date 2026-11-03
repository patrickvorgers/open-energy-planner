"""Independent unit, freshness, optional-device and real HTTP adapter contracts."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.open_energy_planner.measurement_sources import (
    HomeAssistantMeasurementSource,
    SourceReadError,
    StateNormalizer,
)
from custom_components.open_energy_planner.measurements import Quantity, SourceBinding

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
BINDING = SourceBinding("grid.power", "sensor.grid", Quantity.POWER)


def state(value="1.25", unit="kW", **changes):
    """Build a plain HA sensor-state fixture with explicit units and timestamp metadata."""
    return {
        "entity_id": "sensor.grid",
        "state": value,
        "attributes": {"unit_of_measurement": unit},
        "last_updated": NOW.isoformat(),
        **changes,
    }


@pytest.mark.parametrize(
    "quantity,value,unit,expected,canonical",
    [
        (Quantity.POWER, "1.25", "kW", 1250, "W"),
        (Quantity.POWER, "-250", "W", -250, "W"),
        (Quantity.ENERGY, "1250", "Wh", 1.25, "kWh"),
        (Quantity.ENERGY, "2", "kWh", 2, "kWh"),
        (Quantity.ENERGY, "0.25", "MWh", 250, "kWh"),
        (Quantity.SOC, "72.5", "%", 72.5, "%"),
        (Quantity.POWER, "0", "W", 0, "W"),
    ],
)
def test_units(quantity, value, unit, expected, canonical):
    """Normalize supported power/energy/SoC units. Require the independently specified W/kWh/percent values."""
    batch = StateNormalizer().normalize(
        [state(value, unit)], [replace(BINDING, quantity=quantity)], NOW
    )
    assert batch.ready
    assert batch.readings[0].value == expected
    assert batch.readings[0].unit == canonical
    with pytest.raises(FrozenInstanceError):
        batch.readings[0].value = 0


def test_explicit_sign_translation_and_no_battery_requirement_without_battery():
    """Invert an explicitly selected power sign. Retain grid-only readiness without any implicit battery source."""
    batch = StateNormalizer().normalize([state()], [replace(BINDING, multiplier=-1)], NOW)
    assert batch.ready
    assert len(batch.readings) == 1
    assert batch.readings[0].value == -1250


@pytest.mark.parametrize(
    "payload,reason",
    [
        ([], "missing_entity"),
        ([state("unknown")], "unavailable"),
        ([state("unavailable")], "unavailable"),
        ([state("NaN")], "invalid_number"),
        ([state("inf")], "invalid_number"),
        ([state(True)], "invalid_number"),
        ([state("broken")], "invalid_number"),
        ([state(unit="kWh")], "unsupported_unit"),
        ([state(attributes=None)], "invalid_attributes"),
        ([state(last_updated="bad")], "invalid_timestamp"),
        ([state(last_updated="2026-10-19T12:00:00")], "invalid_timestamp"),
        ([state(last_updated=(NOW + timedelta(seconds=1)).isoformat())], "future_measurement"),
    ],
)
def test_required_failures_are_explicit_and_never_zero(payload, reason):
    """Supply missing/unusable required data. Emit a blocking source issue rather than fabricating zero power."""
    batch = StateNormalizer().normalize(payload, [BINDING], NOW)
    assert not batch.ready
    assert batch.readings == ()
    assert batch.issues[0].reason == reason
    optional = StateNormalizer().normalize(payload, [replace(BINDING, required=False)], NOW)
    assert optional.ready
    assert optional.issues[0].reason == reason


@pytest.mark.parametrize("value", ["-1", "101"])
def test_soc_range(value):
    """Supply SoC below zero or above 100 percent. Reject the reading with an explicit source issue."""
    batch = StateNormalizer().normalize(
        [state(value, "%")], [replace(BINDING, quantity=Quantity.SOC)], NOW
    )
    assert batch.issues[0].reason == "invalid_soc"


def test_negative_energy_rejected():
    """Provide negative cumulative energy. Report invalid input instead of accepting an impossible counter."""
    batch = StateNormalizer().normalize(
        [state("-1", "kWh")], [replace(BINDING, quantity=Quantity.ENERGY)], NOW
    )
    assert batch.issues[0].reason == "negative_energy"


def test_device_sample_timestamp_is_not_hidden_by_recent_state_update():
    """An old sample attribute is retained without rejecting a valid current HA state. Malformed metadata still fails."""
    sample = state()
    sample["attributes"]["sampled_at"] = (NOW - timedelta(hours=1)).isoformat()
    batch = StateNormalizer().normalize(
        [sample], [replace(BINDING, timestamp_attribute="sampled_at")], NOW
    )
    assert batch.ready
    assert batch.readings[0].observed_at == NOW - timedelta(hours=1)
    assert batch.readings[0].timestamp_kind == "device_sample"
    del sample["attributes"]["sampled_at"]
    assert (
        StateNormalizer()
        .normalize([sample], [replace(BINDING, timestamp_attribute="sampled_at")], NOW)
        .issues[0]
        .reason
        == "invalid_timestamp"
    )


def test_last_changed_does_not_determine_freshness_and_inputs_are_not_retained():
    """Separate value-change time from reporting time. Use current reporting freshness without mutating input state."""
    sample = state(last_changed="2020-01-01T00:00:00Z")
    batch = StateNormalizer().normalize([sample], [BINDING], NOW)
    sample["state"] = "999"
    assert batch.ready
    assert batch.readings[0].value == 1250


@pytest.mark.parametrize(
    "quantity,value,unit",
    [(Quantity.POWER, "0", "W"), (Quantity.ENERGY, "123", "kWh"), (Quantity.SOC, "10", "%")],
)
def test_unchanged_available_ha_values_keep_original_times_and_remain_usable(quantity, value, unit):
    """Eight-hour-old unchanged values are valid current HA states, not proof of a failed device."""
    old = (NOW - timedelta(hours=8)).isoformat()
    sample = state(value, unit, last_reported=old, last_updated=old)
    batch = StateNormalizer().normalize([sample], [replace(BINDING, quantity=quantity)], NOW)
    assert batch.ready
    assert batch.captured_at == NOW
    assert batch.readings[0].value == float(value)
    assert batch.readings[0].observed_at == NOW - timedelta(hours=8)
    assert batch.readings[0].reported_at == NOW - timedelta(hours=8)


@pytest.mark.parametrize("payload", [{}, [None], [state(), state()]])
def test_malformed_or_ambiguous_payload_rejected(payload):
    """Corrupt state records or duplicate identities. Reject the malformed capture instead of guessing a source."""
    with pytest.raises(SourceReadError):
        StateNormalizer().normalize(payload, [BINDING], NOW)


def test_duplicate_binding_keys_rejected():
    """Select duplicate logical source keys. Reject ambiguous ownership before normalization."""
    with pytest.raises(ValueError, match="unique"):
        StateNormalizer().normalize([state()], [BINDING, BINDING], NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_id": "sensor.grid/../../services"},
        {"multiplier": 0},
        {"multiplier": True},
        {"quantity": Quantity.SOC, "multiplier": -1},
        {"maximum_age_s": 0},
        {"maximum_age_s": -1},
        {"maximum_age_s": float("nan")},
        {"maximum_age_s": True},
        {"maximum_age_s": False},
        {"maximum_age_s": "300"},
        {"maximum_age_s": None},
        {"required": "yes"},
        {"maximum_age_s": 10**1000},
        {"key": ["grid.power"]},
        {"key": {"name": "grid.power"}},
        {"key": True},
        {"key": 12},
        {"key": " "},
        {"entity_id": ["sensor.grid"]},
        {"entity_id": {"id": "sensor.grid"}},
        {"entity_id": True},
        {"entity_id": None},
        {"quantity": "power"},
    ],
)
def test_invalid_source_configuration(changes):
    """Supply invalid binding fields. Require validation failure before readings are interpreted."""
    with pytest.raises(ValueError):
        replace(BINDING, **changes)


@pytest.mark.parametrize("attribute", [[], ["sampled_at"], {}, 12, True, "", " "])
def test_timestamp_attribute_must_be_a_nonempty_string(attribute):
    """Select an invalid device timestamp key. Reject configuration rather than falling back silently."""
    with pytest.raises(ValueError):
        replace(BINDING, timestamp_attribute=attribute)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_utc_timestamp_overflow_is_an_optional_source_issue(stamp):
    """Normalize an overflowing timezone conversion. Report an optional issue without crashing or blocking required inputs."""
    binding = replace(BINDING, required=False, timestamp_attribute="sampled_at")
    sample = state()
    sample["attributes"]["sampled_at"] = stamp
    batch = StateNormalizer().normalize([sample], [binding], NOW)
    assert batch.ready
    assert batch.readings == ()
    assert batch.issues[0].reason == "invalid_timestamp"


def test_bridge_reads_only_selected_states_in_process_without_http_or_ha_token():
    """Read selected registry entries. Require normalized in-process results without a second connection or credentials."""
    from types import SimpleNamespace

    requested = []

    class States:
        def get(self, entity_id):
            requested.append(entity_id)
            return SimpleNamespace(as_dict=lambda: state()) if entity_id == "sensor.grid" else None

    batch = HomeAssistantMeasurementSource(States()).read([BINDING], NOW)
    assert requested == ["sensor.grid"]
    assert batch.ready
    assert batch.readings[0].value == 1250
