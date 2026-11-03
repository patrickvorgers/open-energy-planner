"""Load the shared shipped example profiles without HA or device-service access."""

import json
from pathlib import Path

PROFILES = json.loads(Path(__file__).with_name("appliance_profiles.json").read_text())


POWERS_W = tuple(PROFILES["dishwasher"]["power_w"])


PHASE_SECONDS = 900


DURATION_SECONDS = len(POWERS_W) * PHASE_SECONDS


WATTS_PER_KILOWATT = 1000


SECONDS_PER_HOUR = 3600


HISTORY_CYCLE_HOUR_UTC = 22
