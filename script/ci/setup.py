"""Idempotent VS Code onboarding for the isolated development installation."""

import os
import subprocess
import sys

subprocess.run([sys.executable, "script/ci/check_provenance.py"], check=True)
subprocess.run([sys.executable, "script/ci/check_native.py"], check=True)
subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/frontend"], check=True)
if os.environ.get("OEP_DEV_HA_URL"):
    subprocess.run([sys.executable, "script/home_assistant/bootstrap/provision_ha.py"], check=True)
