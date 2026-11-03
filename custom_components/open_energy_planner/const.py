"""The app and integration are released as one tested pair."""

import json
from pathlib import Path

DOMAIN = "open_energy_planner"
BUILD_INFO = json.loads(Path(__file__).with_name("build-info.json").read_text(encoding="utf-8"))
RELEASE = BUILD_INFO["version"]
PLATFORMS = ["sensor", "switch", "date", "time", "button", "number", "select", "binary_sensor"]
