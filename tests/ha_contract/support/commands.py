"""Explicit current-device execution documents for deterministic schedule and HA tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from open_energy_planner.build_info import RELEASE


def command_plan(now=None, powers=(-1000, None, 0), seconds=30):
    """Declare current-schema battery commands without invoking production planning."""
    now = now or datetime.now(UTC)
    return {
        "schema": 2,
        "device_commands": True,
        "release": RELEASE,
        "plan_id": str(uuid4()),
        "config_revision": "demo-1",
        "generated_at": now.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "feasible",
        "valid_until": (now + timedelta(seconds=seconds * len(powers))).isoformat(),
        "steps": [
            {
                "start": (now + timedelta(seconds=seconds * index)).isoformat(),
                "end": (now + timedelta(seconds=seconds * (index + 1))).isoformat(),
                "grid_active": power is None,
                "devices": {
                    "battery": {
                        "kind": "battery",
                        "mode": "follow_grid_target" if power is None else "fixed_power",
                        "power_w": power,
                    }
                },
            }
            for index, power in enumerate(powers)
        ],
    }
