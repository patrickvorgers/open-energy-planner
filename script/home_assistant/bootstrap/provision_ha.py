"""Provision only the isolated Compose HA, using HA APIs rather than its database."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

CLIENT_ID = "http://localhost:8123/"
TOKEN = os.environ.get("OEP_API_TOKEN", "local-test-only-0123456789abcdef")


async def wait_for_automatic(request, *, attempts=90, delay=1):
    """Await loaded Hassio setup and one real Supervisor-managed OEP projection, including retained entries."""
    for _ in range(attempts):
        entries = await request("GET", "/api/config/config_entries/entry")
        existing = [item for item in entries if item["domain"] == "open_energy_planner"]
        if len(existing) > 1:
            raise RuntimeError("Duplicate development EMS entries")
        platform = [item for item in entries if item["domain"] == "hassio"]
        states = await request("GET", "/api/states")
        if (
            len(platform) == 1
            and platform[0].get("state") == "loaded"
            and len(existing) == 1
            and any(
                state["entity_id"].endswith("open_energy_planner_status")
                and state.get("attributes", {}).get("connection_type") == "supervisor"
                and not state["attributes"].get("connection_issue")
                for state in states
            )
        ):
            return
        await asyncio.sleep(delay)
    raise RuntimeError("Automatic Supervisor connection did not become ready")


async def provision(*, examples_journal=None, example_request=None):
    """Provision/recover only the isolated Compose HA using APIs and reusable local credentials."""
    if examples_journal is not None and example_request is None:
        raise ValueError("Example automations require the current product API session")
    url = os.environ["OEP_DEV_HA_URL"].rstrip("/")
    if urlsplit(url).hostname not in {"homeassistant", "homeassistant-test"}:
        raise ValueError("Provisioning is restricted to the isolated Compose HA services")
    path = Path(os.environ.get("OEP_DEV_STATE", ".local/developer.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        state = json.loads(path.read_text())
    else:
        state = {"username": "developer", "password": secrets.token_urlsafe(24)}
        path.write_text(json.dumps(state, indent=2) + "\n")
        path.chmod(0o600)

    async with ClientSession(timeout=ClientTimeout(total=15)) as session:

        async def request(method, endpoint, **kwargs):
            """Send an isolated-HA API request and require a successful JSON response."""
            async with session.request(method, url + endpoint, **kwargs) as response:
                response.raise_for_status()
                return await response.json()

        for _ in range(120):
            try:
                async with session.get(url + "/api/onboarding") as response:
                    if response.status in (200, 404):
                        steps = await response.json() if response.status == 200 else []
                        break
            except (ClientError, TimeoutError):
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Development HA did not start within two minutes")

        if "refresh_token" in state:
            tokens = await request(
                "POST",
                "/auth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": state["refresh_token"],
                    "client_id": CLIENT_ID,
                },
            )
        else:
            pending = {step["step"] for step in steps if not step["done"]}
            if "user" in pending:
                result = await request(
                    "POST",
                    "/api/onboarding/users",
                    json={
                        "client_id": CLIENT_ID,
                        "name": "Developer",
                        "language": "en",
                        "username": state["username"],
                        "password": state["password"],
                    },
                )
                code = result["auth_code"]
            else:
                # Recover if user creation succeeded before setup was interrupted.
                flow = await request(
                    "POST",
                    "/auth/login_flow",
                    json={
                        "client_id": CLIENT_ID,
                        "handler": ["homeassistant", None],
                        "redirect_uri": CLIENT_ID,
                    },
                )
                result = await request(
                    "POST",
                    "/auth/login_flow/" + flow["flow_id"],
                    json={
                        "client_id": CLIENT_ID,
                        "username": state["username"],
                        "password": state["password"],
                    },
                )
                code = result["result"]
            tokens = await request(
                "POST",
                "/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": CLIENT_ID,
                },
            )
            state["refresh_token"] = tokens["refresh_token"]
            path.write_text(json.dumps(state, indent=2) + "\n")

        session.headers["Authorization"] = "Bearer " + tokens["access_token"]
        for step in steps:
            if step["done"] or step["step"] == "user":
                continue
            body = {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}
            await request("POST", "/api/onboarding/" + step["step"], json=body)

        if os.environ.get("OEP_DEV_ONBOARD_ONLY") == "true":
            print("Isolated development HA onboarding complete. Existing credentials retained.")
            return

        entries = await request("GET", "/api/config/config_entries/entry")
        existing = [item for item in entries if item["domain"] == "open_energy_planner"]
        automatic = os.environ.get("OEP_DEV_AUTOMATIC") == "true"
        if automatic:
            await wait_for_automatic(request)
        elif not existing:
            flow = await request(
                "POST",
                "/api/config/config_entries/flow",
                json={
                    "handler": "open_energy_planner",
                    "show_advanced_options": False,
                },
            )
            result = await request(
                "POST",
                "/api/config/config_entries/flow/" + flow["flow_id"],
                json={"url": "http://app:8099", "token": TOKEN},
            )
            if result["type"] != "create_entry" and not (
                result["type"] == "abort" and result.get("reason") == "already_configured"
            ):
                raise RuntimeError("Development integration could not be configured")
        elif len(existing) != 1:
            raise RuntimeError("Duplicate development EMS entries")

        for _ in range(60):
            states = await request("GET", "/api/states")
            connected = [
                status_record
                for status_record in states
                if status_record["entity_id"].endswith("open_energy_planner_status")
                and (
                    not automatic
                    or status_record.get("attributes", {}).get("connection_type") == "supervisor"
                )
            ]
            active = [
                status_record for status_record in connected if status_record["state"] == "active"
            ]
            if connected and os.environ.get("OEP_DEV_EXPECT_ACTIVE") == "false":
                if examples_journal is not None:
                    from script.home_assistant.bootstrap.provision_ha_examples import (
                        provision as provision_examples,
                    )

                    await provision_examples(
                        session, url, request, examples_journal, app_request=example_request
                    )
                print(f"Test HA connected. Complete app setup. Login credentials: {path}")
                return
            if active:
                print("Development HA ready: one EMS integration, active virtual commands.")
                print(f"Local browser login: developer. Password is in {path} (ignored by Git).")
                return
            await asyncio.sleep(1)
        raise RuntimeError("Development virtual commands did not become active")


if __name__ == "__main__":
    asyncio.run(provision())
