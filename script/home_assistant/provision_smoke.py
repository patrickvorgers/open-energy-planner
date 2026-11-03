"""Verify first-run and repeated API onboarding against an actual clean test HA."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.request import Request, urlopen

TEST_APP_URL = "http://app:8099"
APP_READY_TIMEOUT_SECONDS = 30
APP_REQUEST_TIMEOUT_SECONDS = 5
APP_POLL_SECONDS = 0.05


def fresh_synthetic_plan():
    """Replace potentially expired prior-test commands before exercising independent HA onboarding."""
    token = os.environ.get("OEP_API_TOKEN", "local-test-only-0123456789abcdef")

    def request(route, body=None):
        """Use only the isolated Compose app, with bounded authenticated fixture requests."""
        message = Request(
            TEST_APP_URL + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urlopen(message, timeout=APP_REQUEST_TIMEOUT_SECONDS) as response:
            return json.load(response)

    original = request("/v1/snapshot")
    if original.get("source") != "synthetic":
        raise RuntimeError("Provision smoke requires the isolated synthetic app")
    requested = request("/v1/replan", {})["planning"]["requested_snapshot_id"]
    deadline = monotonic() + APP_READY_TIMEOUT_SECONDS
    while monotonic() < deadline:
        snapshot = request("/v1/snapshot")
        planning = snapshot.get("planning", {})
        if planning.get("requested_snapshot_id") == requested:
            if planning.get("status") == "failed":
                raise RuntimeError("Synthetic onboarding plan failed validation")
            plan = snapshot.get("plan") or {}
            if (
                planning.get("status") == "ready"
                and snapshot.get("enabled") is True
                and plan.get("input_snapshot_id") == requested
                and plan.get("plan_id") != (original.get("plan") or {}).get("plan_id")
                and datetime.fromisoformat(plan["valid_until"]) > datetime.now(UTC)
            ):
                return
        sleep(APP_POLL_SECONDS)
    raise RuntimeError("Fresh synthetic onboarding plan did not become ready")


def main():
    """Exercise onboarding and interrupted-login recovery with an independently fresh test plan."""
    fresh_synthetic_plan()
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-m", "script.home_assistant.bootstrap.provision_ha"], check=True
        )

    # A stopped setup after user creation must recover through the normal login API.
    path = Path(os.environ["OEP_DEV_STATE"])
    state = json.loads(path.read_text())
    account = (state["username"], state["password"])
    del state["refresh_token"]
    path.write_text(json.dumps(state))
    subprocess.run(
        [sys.executable, "-m", "script.home_assistant.bootstrap.provision_ha"], check=True
    )
    restored = json.loads(path.read_text())
    assert (restored["username"], restored["password"]) == account
    assert restored["refresh_token"]

    rejected = subprocess.run(
        [sys.executable, "-m", "script.home_assistant.bootstrap.provision_ha"],
        env={**os.environ, "OEP_DEV_HA_URL": "http://production.invalid:8123"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "restricted to the isolated Compose HA" in rejected.stderr
    print("First-run, repeat, interrupted-setup recovery and non-development rejection passed.")


if __name__ == "__main__":
    main()
