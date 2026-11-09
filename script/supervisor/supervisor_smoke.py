"""Exercise actual Supervisor ingress, model history and update preservation in its private test engine."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from script.provisioning.acceptance import verify_device_examples
from script.supervisor.provision_supervisor import (
    APP_READY_TIMEOUT_SECONDS,
    APP_SLUG,
    DEVELOPER_ROOT,
    DOCKER,
    SUPERVISOR_ROOT,
    ha,
    require_private_engine,
    run,
    wait_for,
)

HTTP_TIMEOUT_SECONDS = 15
LOCAL_UPDATE_VERSION = "0.0.0.dev1"
MIN_SETUP_ESTIMATE_COMPLETE_DAYS = 27
MIN_MODEL_TRAINING_HOURS = 28 * 24
RETAINED_FINE_SAMPLE_OFFSET = timedelta(days=28)
RETAINED_HISTORY_SAMPLE_LENGTH = timedelta(hours=1)


def request(url, token=None, data=None):
    """Read only the isolated app or HA API with bounded private authorization."""
    message = Request(url, data=data, headers={"Authorization": "Bearer " + token} if token else {})
    with urlopen(message, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def app_request(route):
    """Use the actual local app IP and its existing administration authority, without printing it."""
    app = ha("apps", "info", APP_SLUG)
    token = run(*DOCKER, "exec", "app_" + APP_SLUG, "cat", "/data/administration-token")
    return request(f"http://{app['ip_address']}:8099/v1/{route}", token)


def ha_preferences(*, include_history=False):
    """Read native preferences and optionally stable old Recorder samples from the full-scale lab."""
    from aiohttp import ClientSession, ClientTimeout

    account = json.loads((DEVELOPER_ROOT / "developer.json").read_text())
    data = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": account["refresh_token"],
            "client_id": "http://localhost:8123/",
        }
    ).encode()
    token = request("http://homeassistant/auth/token", data=data)["access_token"]

    async def fetch():
        """Use native authenticated WebSocket commands instead of editing Home Assistant storage."""
        async with (
            ClientSession(timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as session,
            session.ws_connect("http://homeassistant/api/websocket") as websocket,
        ):
            assert (await websocket.receive_json())["type"] == "auth_required"
            await websocket.send_json({"type": "auth", "access_token": token})
            assert (await websocket.receive_json())["type"] == "auth_ok"
            results = {}
            for message_id, command in enumerate(
                (
                    "energy/get_prefs",
                    "config/entity_registry/list",
                    "config/device_registry/list",
                    "get_states",
                ),
                start=1,
            ):
                await websocket.send_json({"id": message_id, "type": command})
                response = await websocket.receive_json()
                assert response["success"] is True
                results[command] = response["result"]
            if include_history:
                source_entity = next(
                    row["entity_id"]
                    for row in results["config/entity_registry/list"]
                    if row["platform"] == "oep_test_fixture" and row["unique_id"] == "grid_import"
                )
                seed_document = json.loads(
                    (SUPERVISOR_ROOT / "homeassistant/.storage/oep_test_fixture").read_text()
                )["data"]
                seed_anchor = datetime.fromisoformat(seed_document["anchor"])
                assert seed_anchor.utcoffset() is not None
                results["recorder_history"] = {}
                # Day 28 is older than compact fixtures but within native fine-history retention.
                for period, offset, expected_count in (
                    ("hour", timedelta(), 1),
                    ("5minute", RETAINED_FINE_SAMPLE_OFFSET, 12),
                ):
                    message_id += 1
                    sample_start = seed_anchor + offset
                    await websocket.send_json(
                        {
                            "id": message_id,
                            "type": "recorder/statistics_during_period",
                            "start_time": sample_start.isoformat(),
                            "end_time": (sample_start + RETAINED_HISTORY_SAMPLE_LENGTH).isoformat(),
                            "statistic_ids": [source_entity],
                            "period": period,
                            "units": {"energy": "kWh"},
                            "types": ["sum", "state"],
                        }
                    )
                    response = await websocket.receive_json()
                    assert response["success"] is True
                    sample_rows = response["result"].get(source_entity, [])
                    assert len(sample_rows) == expected_count, f"Missing retained {period} history"
                    assert all(
                        type(row[field]) in (int, float) and math.isfinite(row[field])
                        for row in sample_rows
                        for field in ("sum", "state")
                    )
                    results["recorder_history"][period] = sample_rows
            return results

    return asyncio.run(fetch())


def database_rows():
    """Read all SQLite application tables in one transaction without exporting their contents."""
    database = SUPERVISOR_ROOT / "apps/data" / APP_SLUG / "archive.sqlite3"
    result = {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            quoted = '"' + table.replace('"', '""') + '"'
            result[table] = sorted(
                hashlib.sha256(repr(row).encode()).hexdigest()
                for row in connection.execute("SELECT * FROM " + quoted)
            )
    assert result and "oep_archive_plans" in result
    return result


def pending_model_forecasts():
    """Read original fitted alternatives from the isolated app's retained daily capture pointers.

    These are actual app-written predictions. Reading them neither creates
    historical forecasts nor supplies admission evidence for a completed day.
    """
    database = SUPERVISOR_ROOT / "apps/data" / APP_SLUG / "archive.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT payload FROM oep_documents WHERE namespace = ? AND name LIKE ? ORDER BY name",
            ("demand", "accuracy-pending-%"),
        ).fetchall()
    return [json.loads(row[0])["capture"]["forecasts"] for row in rows]


def model_history():
    """Require real fitted candidates while original forward-day evidence keeps baseline selection.

    The disposable lab has 31 complete history days. Laundry has insufficient
    history and stays inside the household remainder. Heat-pump weather fitting
    must still succeed, independently of its deliberately unpromoted selection.
    """
    setup = app_request("setup")
    estimate = setup["annual_estimate"]
    assert estimate["days"] >= MIN_SETUP_ESTIMATE_COMPLETE_DAYS
    assert estimate["end"] - estimate["start"] == timedelta(days=estimate["days"]).total_seconds()
    required = {"sensor.oep_test_heat_pump_energy", "sensor.oep_test_dishwasher_energy"}
    observed = {}

    def forecast_ready(forecast, model):
        """Require actual trained prediction metadata and the complete 48-hour energy horizon."""
        return (
            forecast.get("model") == model
            and bool(forecast.get("training_version"))
            and datetime.fromisoformat(forecast["issued_at"]) <= datetime.now(UTC)
            and bool(forecast.get("steps"))
            and sum(step["duration_h"] for step in forecast["steps"]) == 48
        )

    def trained():
        """Wait for archived Ridge/weather fits and the corresponding unpromoted partial composition."""
        result = app_request("consumers").get("result") or {}
        records = {row["entity_id"]: row for row in result.get("devices", [])}
        archives = pending_model_forecasts()
        ready = True
        for entity in sorted(required):
            record = records.get(entity, {})
            forecast = record.get("forecast") or {}
            selection = record.get("model_selection") or {}
            alternatives = next(
                (
                    row
                    for row in archives
                    if row.get("baseline", {}).get("configuration_version")
                    == forecast.get("configuration_version")
                    and row.get("baseline", {}).get("version") == forecast.get("version")
                ),
                {},
            )
            weather = alternatives.get("weather") or {}
            observed[entity] = {
                "model": forecast.get("model"),
                "trained": bool(forecast.get("training_version")),
                "weather": forecast_ready(weather, "xgboost-weather-v1"),
                "ridge": forecast_ready(alternatives.get("ridge") or {}, "ridge-v2"),
            }
            if not (
                record.get("status") == "collecting_forward_accuracy"
                and selection.get("selected") == "baseline"
                and selection.get("completed_windows") == 0
                and record.get("weather_features_used") is False
                and forecast_ready(forecast, "weekday-hour-v2")
                and observed[entity]["ridge"]
            ):
                ready = False
            if entity == "sensor.oep_test_heat_pump_energy":
                quality = selection.get("training_quality") or {}
                if not (
                    observed[entity]["weather"]
                    and quality.get("source_hours", 0) >= MIN_MODEL_TRAINING_HOURS
                    and quality.get("used_hours", 0) >= MIN_MODEL_TRAINING_HOURS
                ):
                    ready = False
        if not ready:
            return None
        if not (
            required <= set(records)
            and not {"sensor.oep_test_washing_machine_energy", "sensor.oep_test_dryer_energy"}
            & set(records)
            and set(result.get("separated_entities", [])) == set(records)
            and result.get("status") == "partial_history"
            and result.get("detail")
            == "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
        ):
            return None
        demand = app_request("demand")
        if not (
            demand.get("ridge_status") == "partial_history"
            and forecast_ready(demand.get("forecast") or {}, "consumer-composite-v1")
            and forecast_ready(demand.get("candidate") or {}, "consumer-composite-v1")
        ):
            return None
        return result

    try:
        wait_for(
            trained, "actual history-trained appliance alternatives and daily baseline selection"
        )
    except RuntimeError as error:
        raise RuntimeError(
            "Actual model readiness: " + json.dumps(observed, sort_keys=True)
        ) from error


def workspace_frame(page):
    """Find the product frame, retrying when HA navigation detaches a frame during the poll."""
    from playwright.sync_api import Error

    try:
        return next(
            (frame for frame in page.frames if frame.locator(".workspace-shell").count()), None
        )
    except Error:
        return None


def device_examples():
    """Require real persisted lab configuration and the native Supervisor sidebar option."""
    assert ha("apps", "info", APP_SLUG)["ingress_panel"] is True
    verify_device_examples(app_request, ha_preferences()["get_states"])


def browser_ingress():
    """Use actual HA login and nested app ingress on desktop/mobile without an OEP token prompt."""
    from playwright.sync_api import Error, expect, sync_playwright

    account = json.loads((DEVELOPER_ROOT / "developer.json").read_text())
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto("http://homeassistant/app/" + APP_SLUG)
                page.locator('input[name="username"]').fill(account["username"])
                page.locator('input[name="password"]').fill(account["password"])
                page.get_by_role("button", name="Log in", exact=True).click()

                workspace = wait_for(lambda: workspace_frame(page), "actual HA ingress")
                content = workspace.frame_locator("#workspace-content")
                expect(content.get_by_role("heading", name="Planning", exact=True)).to_be_visible(
                    timeout=30000
                )
                assert not content.get_by_label("Installation API token", exact=True).is_visible()
                page.screenshot(path="results/ci/supervisor-ingress-desktop.png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                workspace.get_by_role("button", name="Menu", exact=True).click()
                workspace.get_by_role("link", name="Installation", exact=True).click()
                expect(
                    content.get_by_role("button", name="Edit installation", exact=True)
                ).to_be_visible(timeout=30000)
                assert not content.get_by_label("Installation API token", exact=True).is_visible()
                page.screenshot(path="results/ci/supervisor-ingress-mobile.png", full_page=True)
            finally:
                browser.close()
    except Error:
        # Browser call logs can include a filled password. Keep test credentials private on failure.
        raise RuntimeError("Actual Supervisor desktop/mobile ingress verification failed") from None


def retained_state():
    """Capture stable saved settings and original plan IDs independently of live forecasts and checkpoints."""
    setup = app_request("setup")
    snapshot = app_request("snapshot")
    core = SUPERVISOR_ROOT / "homeassistant"
    documents = {}
    preferences = ha_preferences(include_history=True)
    for name in ("core.entity_registry", "core.device_registry", "core.config_entries"):
        if name == "core.config_entries":
            value = json.loads((core / ".storage" / name).read_text())["data"]
            value = [
                {key: row.get(key) for key in ("entry_id", "unique_id", "data", "options")}
                for row in value["entries"]
                if row["domain"] == "open_energy_planner"
            ]
        elif name == "core.entity_registry":
            value = [
                {key: row.get(key) for key in ("entity_id", "unique_id", "platform", "device_id")}
                for row in preferences["config/entity_registry/list"]
            ]
        else:
            value = [
                {key: row.get(key) for key in ("id", "identifiers")}
                for row in preferences["config/device_registry/list"]
            ]
        identity_key = {
            "core.entity_registry": "entity_id",
            "core.device_registry": "id",
            "core.config_entries": "entry_id",
        }[name]
        documents[name] = {
            row[identity_key]: hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            for row in value
        }
    return {
        "instance_id": snapshot["instance_id"],
        "configuration": {key: setup[key] for key in ("installation", "solar", "demand")},
        "example_settings": {
            "consumers": sorted(
                app_request("consumers")["selected"], key=lambda row: row["entity_id"]
            ),
            "ev": app_request("ev")["configuration_version"],
            "thermal": app_request("thermal")["configuration_version"],
            "contract": app_request("contracts")["version"],
        },
        "registry": documents,
        "energy": preferences["energy/get_prefs"],
        "recorder_history": preferences["recorder_history"],
        "administration_token": hashlib.sha256(
            (SUPERVISOR_ROOT / "apps/data" / APP_SLUG / "administration-token").read_bytes()
        ).hexdigest(),
        "files": {
            name: hashlib.sha256((core / name).read_bytes()).hexdigest()
            for name in ("configuration.yaml", "automations.yaml")
        },
        "plans": database_rows()["oep_archive_plans"],
    }


def verify_retained():
    """Require saved state equality and retention of every original plan after a real restart/update."""
    before = json.loads((DEVELOPER_ROOT / "acceptance.json").read_text())
    after = retained_state()
    assert set(before.pop("plans")) <= set(after.pop("plans"))
    for name, rows in before.pop("registry").items():
        current = after["registry"][name]
        assert all(current.get(identity) == digest for identity, digest in rows.items()), name
    del after["registry"]
    differences = sorted(key for key in before if before[key] != after[key])
    assert not differences, f"Saved state changed: {differences}"
    model_history()


def update():
    """Perform the actual Supervisor update while stopped, proving every table survives before resuming."""
    revision = os.environ["OEP_TEST_SOURCE_REVISION"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Acceptance requires the exact tested Git commit")
    ha("apps", "stop", APP_SLUG)
    # Supervisor caches Docker events and update restores its observed pre-update state.
    wait_for(
        lambda: ha("apps", "info", APP_SLUG)["state"] == "stopped",
        "stopped app before update snapshot",
        timeout=APP_READY_TIMEOUT_SECONDS,
    )
    before = database_rows()
    app = SUPERVISOR_ROOT / "apps/local/open-energy-planner"
    integration = SUPERVISOR_ROOT / "homeassistant/custom_components/open_energy_planner"
    for path in (app / "config.yaml", integration / "manifest.json"):
        metadata = json.loads(path.read_text())
        metadata["version"] = LOCAL_UPDATE_VERSION
        path.write_text(json.dumps(metadata) + "\n")
    identity = {
        "version": LOCAL_UPDATE_VERSION,
        "build": None,
        "revision": revision,
        "release_tag": None,
    }
    for directory in (app / "src/open_energy_planner", integration):
        (directory / "build-info.json").write_text(json.dumps(identity) + "\n")
    ha("store", "reload")
    assert ha("apps", "info", APP_SLUG)["update_available"]
    ha("apps", "update", APP_SLUG)
    wait_for(
        lambda: ha("apps", "info", APP_SLUG)["state"] == "stopped",
        "stopped updated app before database comparison",
        timeout=APP_READY_TIMEOUT_SECONDS,
    )
    assert database_rows() == before
    ha("core", "restart")
    ha("apps", "start", APP_SLUG)

    def matched():
        """Require loaded app identity and current integration contact after both components restart."""
        result = app_request("version")
        return result if result["status"] == "matched" else None

    version = wait_for(matched, "matched updated app/integration")
    assert version["app"] == identity
    assert version["integration"] == {"version": LOCAL_UPDATE_VERSION, "build": None}
    verify_retained()
    browser_ingress()


def main():
    """Keep acceptance mutations confined to an explicitly marked, privately owned test engine."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
        raise RuntimeError("Supervisor acceptance requires its dedicated isolated test invocation")
    require_private_engine()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("capture", "retained", "update"))
    phase = parser.parse_args().phase
    if phase == "capture":
        device_examples()
        model_history()
        browser_ingress()
        (DEVELOPER_ROOT / "acceptance.json").write_text(json.dumps(retained_state()))
    elif phase == "retained":
        verify_retained()
    else:
        update()
    print(f"Actual Supervisor acceptance: {phase} passed.")


if __name__ == "__main__":
    main()
