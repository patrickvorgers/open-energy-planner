"""Followed cycles take authority before legacy overlap reconciliation."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.evidence import refine
from open_energy_planner.appliances.cycles.model import CycleProfile, restore_profile
from tests.app.appliance.support import ENTITY, NOW, POWER


def raw_record(identity, start, hours, power_w, *, observed=False, intervals=4):
    """Archive a constant-power run with independently integrated counter samples."""
    duration = timedelta(hours=hours)
    return {
        "id": identity,
        "entity_id": ENTITY,
        "power_entity": POWER,
        "started_at": start.isoformat(),
        "completed_at": (start + duration).isoformat(),
        "synthetic": False,
        "quality": "recorder_state",
        "observation_coverage": observed,
        "points": [
            {
                "at": (start + duration * index / intervals).isoformat(),
                "kwh": 10 + power_w / 1000 * hours * index / intervals,
            }
            for index in range(intervals + 1)
        ],
        "profile": asdict(CycleProfile((power_w,) * (hours * 4))),
    }


@pytest.mark.parametrize("observed_runs", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
def test_observed_authority_precedes_dense_or_bridging_legacy_ranges(observed_runs, reverse):
    """One dense old view cannot replace or join distinct followed runs or drop explicit energy."""
    observed = [
        raw_record(f"observed-{index}", NOW + timedelta(hours=2 * index), 1, 1000, observed=True)
        for index in range(observed_runs)
    ]
    legacy = raw_record(
        "legacy", NOW - timedelta(hours=1), observed_runs * 2 + 1, 9000, intervals=60
    )
    explicit = {
        **raw_record("explicit", NOW - timedelta(hours=3), 1, 3000),
        "quality": "explicit_completion",
    }
    records = [legacy, *observed, explicit]
    if reverse:
        records.reverse()
    original = deepcopy(records)
    settings = {}

    refine(settings, records)

    learned = restore_profile(settings["profile"])
    assert learned.samples == observed_runs + 1
    assert learned.energy_kwh == pytest.approx((observed_runs + 3) / (observed_runs + 1))
    assert learned.power_w == pytest.approx((learned.energy_kwh * 1000,) * 4)
    assert settings["profile_basis"] == "observed"
    assert settings["profile_sources"] == {"hourly_cycles": 0, "fine_cycles": observed_runs + 1}
    assert {record["id"] for record in settings["recent_cycles"]} == {
        "explicit",
        *(record["id"] for record in observed),
    }
    assert [
        {key: value for key, value in record.items() if key != "_profile"} for record in records
    ] == original
