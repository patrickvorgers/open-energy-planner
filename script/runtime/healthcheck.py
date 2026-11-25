"""Synthetic readiness or reachable HA-mode setup. Never confuse missing data with a crash."""

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HEALTHCHECK_TIMEOUT_SECONDS = 4


def check(base, token):
    """Treat an authenticated, reachable maintenance host as intentionally paused, not crashed."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with urlopen(
            Request(base + "/v1/readiness", headers=headers), timeout=HEALTHCHECK_TIMEOUT_SECONDS
        ) as response:
            readiness = json.load(response)
    except HTTPError as error:
        code = error.code
        error.close()
        if code != 503:
            raise
        with urlopen(
            Request(base + "/v1/storage/migration", headers=headers),
            timeout=HEALTHCHECK_TIMEOUT_SECONDS,
        ) as response:
            status = json.load(response)
        if status.get("running") is not False or status.get("phase") not in {
            "preparing",
            "copying",
            "verifying",
            "checking",
            "restarting",
            "failed",
            "interrupted",
        }:
            raise SystemExit("Invalid maintenance status")
        return
    if readiness.get("source") == "home_assistant":
        if readiness.get("planning_status") not in (
            "not_ready",
            "calculating",
            "ready",
            "failed",
        ):
            raise SystemExit("Invalid HA-mode status")
    elif readiness.get("plan_available") is not True:
        raise SystemExit("No validated plan yet")


if __name__ == "__main__":
    check("http://127.0.0.1:8099", os.environ["OEP_API_TOKEN"])
