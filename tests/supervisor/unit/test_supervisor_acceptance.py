"""The slow Supervisor acceptance path must never acquire a persistent lab or hide data changes."""

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.supervisor import check_supervisor, provision_supervisor, supervisor_smoke
from script.supervisor.diagnostics import redact_diagnostics


@pytest.mark.parametrize(
    "secret_field",
    [
        '"password": "two words"',
        "'client_secret': 'two words'",
        json.dumps({"token": 'two "quoted" words\\suffix'}),
        "password=two words with spaces",
        "Authorization: Bearer two words",
    ],
)
def test_supervisor_diagnostics_redact_entire_secret_values(secret_field):
    """Quoted, escaped and unquoted secrets cannot leak suffixes while other lines remain useful."""
    diagnostic = redact_diagnostics(f"Operation rejected\n{secret_field}\nRecovery available")
    assert "Operation rejected" in diagnostic
    assert "Recovery available" in diagnostic
    assert "[redacted credential]" in diagnostic
    for secret_fragment in ("two", "words", "quoted", "suffix", "spaces"):
        assert secret_fragment not in diagnostic


def test_failed_acceptance_diagnostics_are_bounded_and_redacted(tmp_path, monkeypatch):
    """Store reload diagnostics retain the error while excluding credentials from captured output."""
    calls = []

    def run(command, **options):
        """Expose private-looking test credentials only at the subprocess capture boundary."""
        calls.append((command, options))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "ERROR [supervisor.store] Repository reload failed\n"
                "https://private:credential@example.invalid/store\n"
                "Authorization: Bearer short-private-value\n"
                "token=another-private-value password=small-secret\n"
                "access_token=short-value SUPERVISOR_TOKEN=short-supervisor\n"
                "refresh_token: short-refresh client_secret=short-client\n" + "a" * 64 + "\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(check_supervisor.subprocess, "run", run)
    compose = ["docker", "compose", "-p", "oep-supervisor-check-owned"]
    check_supervisor.capture_failure(compose, tmp_path)
    command, options = calls[0]
    assert command[: len(compose)] == compose
    assert command[-4:] == ["logs", "--tail", "200", "hassio_supervisor"]
    assert options["timeout"] > 0 and options["capture_output"] and not options["check"]
    report = (tmp_path / "supervisor-failure.log").read_text(encoding="utf-8")
    assert "Repository reload failed" in report
    for credential in (
        "private:credential",
        "short-private-value",
        "another-private-value",
        "small-secret",
        "short-value",
        "short-supervisor",
        "short-refresh",
        "short-client",
        "a" * 64,
    ):
        assert credential not in report


@pytest.mark.parametrize("failure_kind", ["exit", "response"])
def test_supervisor_cli_failures_preserve_redacted_response(monkeypatch, failure_kind):
    """CLI exit and protocol errors remain fatal with useful details but no credentials or command arguments."""
    response = json.dumps(
        {
            "result": "error",
            "message": "Fixture store reload rejected",
            "token": "short-private-token",
            "api_key": "small-key",
            "url": "https://private:credential@example.invalid/store",
            "database_url": "postgresql+psycopg://db-user:db-secret@db.invalid/oep",
        }
    )

    def failed_command(*arguments):
        """Represent the CLI's actual stdout and stderr while keeping all execution inside the unit test."""
        if failure_kind == "exit":
            raise subprocess.CalledProcessError(
                1, list(arguments), output=response, stderr="Authorization: Bearer short-secret"
            )
        return response

    monkeypatch.setattr(provision_supervisor, "run", failed_command)
    with pytest.raises(RuntimeError, match="apps info") as caught:
        provision_supervisor.ha("apps", "info", "--private-option=sentinel-private-argument")
    diagnostic = str(caught.value)
    assert "Fixture store reload rejected" in diagnostic
    for secret in (
        "short-private-token",
        "private:credential",
        "short-secret",
        "db-user:db-secret",
        "small-key",
        "sentinel-private-argument",
    ):
        assert secret not in diagnostic


def test_supervisor_cli_failure_response_is_bounded(monkeypatch):
    """Large CLI error output cannot flood CI while a failed operation remains a failure."""

    def failed_command(*arguments):
        """Provide a large non-secret error body independent of the implementation's bound."""
        raise subprocess.CalledProcessError(
            1, list(arguments), output="diagnostic context " * 1000, stderr=""
        )

    monkeypatch.setattr(provision_supervisor, "run", failed_command)
    with pytest.raises(RuntimeError) as caught:
        provision_supervisor.ha("store", "reload")
    assert len(str(caught.value).split(": ", 1)[1]) == 4096


@pytest.mark.parametrize("setup_days", [27, 28])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "promoted",
        "weather_missing",
        "fabricated_laundry",
        "short_setup",
        "gapped_setup",
        "wrong_baseline",
        "missing_training",
        "pending_weather",
        "ridge_missing",
        "missing_composite",
        "private_detail",
        "additional_ready",
        "missing_required",
        "separation_missing",
        "stale_archive",
    ],
)
def test_example_model_acceptance_retains_baseline_and_fitted_candidates(
    monkeypatch, invalid, setup_days
):
    """Require fitted archived alternatives without admitting them before actual forward daily evidence."""
    from copy import deepcopy

    baseline = {
        "model": "weekday-hour-v2",
        "configuration_version": "device-boundary",
        "training_version": "actual-history",
        "issued_at": "2026-01-01T00:00:00+00:00",
        "steps": [{"start": "2026-01-01T00:00:00+00:00", "duration_h": 48, "kwh": 4.8}],
    }
    records = [
        {
            "entity_id": "sensor.oep_test_" + name + "_energy",
            "status": "collecting_forward_accuracy",
            "forecast": {**baseline, "configuration_version": name, "version": name + "-original"},
            "weather_features_used": False,
            "model_selection": {
                "selected": "baseline",
                "completed_windows": 0,
                "training_quality": {"source_hours": 744, "used_hours": 720},
            },
        }
        for name in ("heat_pump", "dishwasher")
    ]
    archives = [
        {
            "baseline": record["forecast"],
            "ridge": {**record["forecast"], "model": "ridge-v2"},
            **(
                {"weather": {**record["forecast"], "model": "xgboost-weather-v1"}}
                if record["entity_id"] == "sensor.oep_test_heat_pump_energy"
                else {}
            ),
        }
        for record in records
    ]
    complete_archives = deepcopy(archives)
    detail = (
        "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
    )
    result = {
        "devices": records,
        "status": "partial_history",
        "detail": detail,
        "separated_entities": [record["entity_id"] for record in records],
    }
    days = 26 if invalid == "short_setup" else setup_days
    responses = {
        "setup": {
            "annual_estimate": {
                "days": days,
                "start": 20000 * 86400,
                "end": (20000 + days + (invalid == "gapped_setup")) * 86400,
            }
        },
        "consumers": {"result": result},
        "demand": {
            "forecast": {**baseline, "model": "consumer-composite-v1"},
            "candidate": {**baseline, "model": "consumer-composite-v1"},
            "ridge_status": "partial_history",
        },
    }
    if invalid == "additional_ready":
        for name in ("wallbox_a", "wallbox_b", "boiler"):
            records.append(
                {**deepcopy(records[1]), "entity_id": "sensor.oep_test_" + name + "_energy"}
            )
        result["separated_entities"] = [record["entity_id"] for record in records]
    elif invalid == "missing_required":
        records.pop(0)
    elif invalid == "separation_missing":
        result["separated_entities"].pop()
    elif invalid == "stale_archive":
        archives[0]["baseline"] = {**archives[0]["baseline"], "version": "older-original"}
    elif invalid == "promoted":
        records[0]["model_selection"]["selected"] = "weather"
    elif invalid in {"weather_missing", "pending_weather"}:
        del archives[0]["weather"]
    elif invalid == "fabricated_laundry":
        records.append({**records[1], "entity_id": "sensor.oep_test_dryer_energy"})
    elif invalid == "wrong_baseline":
        records[0]["forecast"] = {**records[0]["forecast"], "model": "ridge-v2"}
    elif invalid == "missing_training":
        records[0]["forecast"] = {**records[0]["forecast"], "training_version": None}
    elif invalid == "ridge_missing":
        del archives[1]["ridge"]
    elif invalid == "missing_composite":
        responses["demand"]["candidate"] = None
    elif invalid == "private_detail":
        result["detail"] = "sensor.oep_test_dryer_energy unavailable"
    monkeypatch.setattr(supervisor_smoke, "app_request", lambda route: responses[route])
    monkeypatch.setattr(
        supervisor_smoke, "pending_model_forecasts", lambda: archives, raising=False
    )

    def wait_for_models(operation, _description):
        """Retain asynchronous readiness until the actual weather candidate archive is available."""
        if invalid == "pending_weather":
            assert operation() is None
            archives[:] = complete_archives
        result = operation()
        assert result is not None
        return result

    monkeypatch.setattr(supervisor_smoke, "wait_for", wait_for_models)
    if invalid not in (None, "pending_weather", "additional_ready"):
        with pytest.raises(AssertionError):
            supervisor_smoke.model_history()
    else:
        supervisor_smoke.model_history()


def test_model_readiness_timeout_identifies_untrained_weather_component(monkeypatch):
    """A pending weather-dependent model remains rejected and its actual fallback is visible on timeout."""
    responses = {
        "setup": {"annual_estimate": {"days": 28, "start": 0, "end": 28 * 86400}},
        "consumers": {
            "result": {
                "devices": [
                    {
                        "entity_id": "sensor.oep_test_heat_pump_energy",
                        "forecast": {"model": "weekday-hour-v2", "training_version": "history"},
                        "weather_features_used": False,
                    }
                ]
            }
        },
    }
    monkeypatch.setattr(supervisor_smoke, "app_request", lambda route: responses[route])

    def expired(operation, _description):
        """Observe one incomplete result, then represent the existing bounded platform deadline."""
        assert operation() is None
        raise RuntimeError("fixture readiness deadline")

    monkeypatch.setattr(supervisor_smoke, "pending_model_forecasts", list, raising=False)
    monkeypatch.setattr(supervisor_smoke, "wait_for", expired)
    with pytest.raises(RuntimeError, match="Actual model readiness") as caught:
        supervisor_smoke.model_history()
    assert "sensor.oep_test_heat_pump_energy" in str(caught.value)
    assert "weekday-hour-v2" in str(caught.value)
    assert '"weather": false' in str(caught.value)
    assert str(caught.value.__cause__) == "fixture readiness deadline"


def test_acceptance_does_not_operate_on_an_unmarked_environment(monkeypatch):
    """Calling the mutating acceptance script ordinarily fails before any private-engine query."""
    monkeypatch.delenv("OEP_SUPERVISOR_SMOKE", raising=False)

    def unexpected_engine():
        """No Docker request is allowed before the explicit test opt-in."""
        pytest.fail("The unmarked caller reached Docker")

    monkeypatch.setattr(supervisor_smoke, "require_private_engine", unexpected_engine)
    with pytest.raises(RuntimeError, match="dedicated isolated test invocation"):
        supervisor_smoke.main()


def test_acceptance_failure_cleans_only_its_new_project(tmp_path, monkeypatch):
    """A failed first provision removes only its unique test project's resources, never a persistent lab."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    monkeypatch.setattr(check_supervisor.dev, "ROOT", tmp_path)
    monkeypatch.setattr(check_supervisor.dev, "prepare_acceptance_sources", lambda: None)
    monkeypatch.setattr(check_supervisor.dev, "require_local_engine", lambda: "outer-engine")
    monkeypatch.setattr(check_supervisor, "uuid4", lambda: SimpleNamespace(hex="abcdef1234567890"))
    (tmp_path / ".local").mkdir()
    commands = []

    def output(command, **kwargs):
        """Supply independent immutable-source and outer-engine identities without using Docker."""
        return "a" * 40 if command[0] == "git" else "outer-engine"

    def run(command, **kwargs):
        """Fail the cold provision after recording the invocation's ownership boundary."""
        commands.append(command)
        if "exec" in command:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check_supervisor.subprocess, "check_output", output)
    monkeypatch.setattr(check_supervisor.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        check_supervisor.main()
    assert len(commands) == 5
    assert commands[2][-1] == "hassio_supervisor"
    assert all(
        command[:4] == ["docker", "compose", "-p", "oep-supervisor-check-abcdef123456"]
        for command in commands[:-1]
    )
    assert commands[-2][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert commands[-1] == ["docker", "image", "rm", "oep-supervisor-check-abcdef123456-dev"]


def test_stopped_snapshot_checks_every_application_table(tmp_path, monkeypatch):
    """An additional table with binary/unknown data must participate in equality proof just like archived plans."""
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    data = tmp_path / "apps/data/local_open_energy_planner"
    data.mkdir(parents=True)
    with sqlite3.connect(data / "archive.sqlite3") as connection:
        connection.execute("CREATE TABLE oep_archive_plans (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO oep_archive_plans VALUES ('original')")
        connection.execute("CREATE TABLE future_feature (payload BLOB, value REAL)")
        connection.execute("INSERT INTO future_feature VALUES (?, NULL)", (b"original",))
    before = supervisor_smoke.database_rows()
    assert set(before) == {"oep_archive_plans", "future_feature"}
    assert supervisor_smoke.database_rows() == before
    with sqlite3.connect(data / "archive.sqlite3") as connection:
        connection.execute("UPDATE future_feature SET payload = ?", (b"changed",))
    after = supervisor_smoke.database_rows()
    assert after["oep_archive_plans"] == before["oep_archive_plans"]
    assert after["future_feature"] != before["future_feature"]


def test_update_rejects_an_ambiguous_source_before_stopping_the_app(monkeypatch):
    """A mutable branch name cannot be stamped as the updated component's immutable test provenance."""
    monkeypatch.setenv("OEP_TEST_SOURCE_REVISION", "main")

    def unexpected_supervisor(*arguments):
        """An invalid source identity must fail before any app or Core mutation."""
        pytest.fail("Supervisor was called for an invalid source revision")

    monkeypatch.setattr(supervisor_smoke, "ha", unexpected_supervisor)
    with pytest.raises(ValueError, match="exact tested Git commit"):
        supervisor_smoke.update()


@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("update_stops", [False, True])
def test_update_observes_stopped_state_before_both_database_snapshots(
    tmp_path, monkeypatch, delayed, update_stops
):
    """Wait for stopped-state confirmation before each database snapshot.

    The stuck post-update case takes the baseline snapshot, then times out before
    the second database read and any restart or completion checks.
    """
    revision = "a" * 40
    monkeypatch.setenv("OEP_TEST_SOURCE_REVISION", revision)
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    app = tmp_path / "apps/local/open-energy-planner"
    integration = tmp_path / "homeassistant/custom_components/open_energy_planner"
    (app / "src/open_energy_planner").mkdir(parents=True)
    integration.mkdir(parents=True)
    for path in (app / "config.yaml", integration / "manifest.json"):
        path.write_text('{"version": "old"}')
    elapsed_seconds = 0

    def advance_clock(seconds):
        """Advance real polling delays deterministically without waiting on the wall clock."""
        nonlocal elapsed_seconds
        elapsed_seconds += seconds

    monkeypatch.setattr(provision_supervisor, "sleep", advance_clock)
    monkeypatch.setattr(provision_supervisor, "monotonic", lambda: elapsed_seconds)
    phase = "running"
    pending_states = iter(())
    snapshots = []

    def supervisor(*arguments):
        """Model cached state lag and the upstream update's restoration of its observed initial state."""
        nonlocal phase, pending_states
        if arguments[:2] == ("apps", "stop"):
            phase = "stopping"
            pending_states = iter(["started", "stopped"] if delayed else ["stopped"])
        elif arguments[:2] == ("apps", "info"):
            if phase == "updating" and not update_stops:
                return {"state": "started", "update_available": True}
            if phase in ("stopping", "updating"):
                state = next(pending_states)
                if state == "stopped":
                    phase = "stopped" if phase == "stopping" else "updated_stopped"
                return {"state": state, "update_available": True}
            return {"state": "stopped", "update_available": True}
        elif arguments[:2] == ("apps", "update"):
            assert phase == "stopped" and snapshots == ["stopped"]
            phase = "updating"
            pending_states = iter(["unknown", "stopped"] if delayed else ["stopped"])
        elif arguments[:2] == ("core", "restart"):
            assert phase == "updated_stopped"
            assert snapshots == ["stopped", "updated_stopped"]
        elif arguments[:2] not in (("store", "reload"), ("apps", "start")):
            pytest.fail("Unexpected Supervisor operation")
        return {}

    def snapshot():
        """Require quiescence before reading either complete database image."""
        assert phase in ("stopped", "updated_stopped")
        snapshots.append(phase)
        return {"retained_table": ["original record"]}

    monkeypatch.setattr(supervisor_smoke, "ha", supervisor)
    monkeypatch.setattr(supervisor_smoke, "database_rows", snapshot)
    monkeypatch.setattr(
        supervisor_smoke,
        "app_request",
        lambda _route: {
            "status": "matched",
            "app": {
                "version": "0.0.0.dev1",
                "build": None,
                "revision": revision,
                "release_tag": None,
            },
            "integration": {"version": "0.0.0.dev1", "build": None},
        },
    )
    verified = []
    monkeypatch.setattr(supervisor_smoke, "verify_retained", lambda: verified.append("retained"))
    monkeypatch.setattr(supervisor_smoke, "browser_ingress", lambda: verified.append("ingress"))
    if update_stops:
        supervisor_smoke.update()
        assert snapshots == ["stopped", "updated_stopped"]
        assert verified == ["retained", "ingress"]
    else:
        with pytest.raises(RuntimeError, match="stopped updated app before database comparison"):
            supervisor_smoke.update()
        assert snapshots == ["stopped"]
        assert verified == [] and phase == "updating"


def test_update_stop_timeout_prevents_snapshot_and_source_changes(tmp_path, monkeypatch):
    """Never continue with an app whose stopped state has not been observed within the bound."""
    monkeypatch.setenv("OEP_TEST_SOURCE_REVISION", "a" * 40)
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    clock = iter([0, 0, 181])
    monkeypatch.setattr(provision_supervisor, "monotonic", lambda: next(clock))
    monkeypatch.setattr(provision_supervisor, "sleep", lambda _seconds: None)
    operations = []

    def supervisor(*arguments):
        """Accept a stop request but never publish its stopped state."""
        operations.append(arguments[:2])
        return {"state": "started"}

    def unexpected_snapshot():
        """Refuse reading the database while a writer may still be active."""
        pytest.fail("Database read before stopped state")

    monkeypatch.setattr(supervisor_smoke, "ha", supervisor)
    monkeypatch.setattr(supervisor_smoke, "database_rows", unexpected_snapshot)
    with pytest.raises(RuntimeError, match="stopped app before update snapshot"):
        supervisor_smoke.update()
    assert operations == [("apps", "stop"), ("apps", "info")]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["report", "compose_down", "image_remove", "image_status"])
@pytest.mark.parametrize("primary_failure", [False, True])
def test_acceptance_cleanup_attempts_every_owned_resource_despite_an_earlier_cleanup_failure(
    tmp_path, monkeypatch, failure, primary_failure
):
    """All cleanup runs and its failures cannot mask a primary acceptance failure."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    monkeypatch.setattr(check_supervisor.dev, "ROOT", tmp_path)
    monkeypatch.setattr(check_supervisor.dev, "prepare_acceptance_sources", lambda: None)
    monkeypatch.setattr(check_supervisor.dev, "require_local_engine", lambda: "outer-engine")
    monkeypatch.setattr(check_supervisor, "uuid4", lambda: SimpleNamespace(hex="abcdef1234567890"))
    monkeypatch.setattr(
        check_supervisor.subprocess, "check_output", lambda *args, **kwargs: "a" * 40
    )
    (tmp_path / ".local").mkdir()
    commands = []
    original_write = Path.write_text

    def write(path, *arguments, **kwargs):
        """Fail only timing-report output, leaving temporary Compose setup intact."""
        if failure == "report" and path.name == "supervisor-timings.json":
            raise OSError("Report directory became unavailable")
        return original_write(path, *arguments, **kwargs)

    def run(command, **kwargs):
        """Record cleanup attempts and simulate a daemon timeout during Compose down."""
        commands.append(command)
        if primary_failure and "exec" in command:
            raise ValueError("Original acceptance failure")
        if failure == "compose_down" and "down" in command:
            raise subprocess.TimeoutExpired(command, 1200)
        if failure == "image_remove" and command[:3] == ["docker", "image", "rm"]:
            raise subprocess.TimeoutExpired(command, 60)
        if failure == "image_status" and command[:3] == ["docker", "image", "rm"]:
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(check_supervisor.subprocess, "run", run)
    expected = (
        ValueError
        if primary_failure
        else {
            "report": OSError,
            "compose_down": subprocess.TimeoutExpired,
            "image_remove": subprocess.TimeoutExpired,
            "image_status": subprocess.CalledProcessError,
        }[failure]
    )
    with pytest.raises(expected) as captured:
        check_supervisor.main()
    if primary_failure:
        assert str(captured.value) == "Original acceptance failure"
        assert any("Supervisor cleanup failed:" in note for note in captured.value.__notes__)
    assert commands[-2][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert commands[-1] == ["docker", "image", "rm", "oep-supervisor-check-abcdef123456-dev"]


def test_retained_state_detects_replaced_or_missing_administration_token(tmp_path, monkeypatch):
    """Detect changed authority, old Recorder evidence or consumers while ignoring transient catalogue versions."""
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    core = tmp_path / "homeassistant"
    (core / ".storage").mkdir(parents=True)
    (core / ".storage/core.config_entries").write_text(json.dumps({"data": {"entries": []}}))
    for name in ("configuration.yaml", "automations.yaml"):
        (core / name).write_text("original")
    token = tmp_path / "apps/data/local_open_energy_planner/administration-token"
    token.parent.mkdir(parents=True)
    token.write_bytes(b"original-private-authority")
    original_consumers = [
        {"entity_id": "sensor.washer", "weather_sensitive": False},
        {"entity_id": "sensor.heat_pump", "weather_sensitive": True},
    ]
    consumers = {"selected": original_consumers, "version": "initial-catalogue"}
    history = {
        "hour": [{"start": 100, "sum": 50, "state": 1050}],
        "5minute": [{"start": 200, "sum": 75, "state": 1075}],
    }
    monkeypatch.setattr(
        supervisor_smoke,
        "app_request",
        lambda route: (
            {"instance_id": "original"}
            if route == "snapshot"
            else consumers
            if route == "consumers"
            else {
                "installation": {},
                "solar": {},
                "demand": {},
                "version": "saved",
                "configuration_version": "saved",
            }
        ),
    )
    monkeypatch.setattr(
        supervisor_smoke,
        "ha_preferences",
        lambda **kwargs: {
            "energy/get_prefs": {},
            "config/entity_registry/list": [],
            "config/device_registry/list": [],
            "recorder_history": json.loads(json.dumps(history)),
        },
    )
    monkeypatch.setattr(supervisor_smoke, "database_rows", lambda: {"oep_archive_plans": []})
    before = supervisor_smoke.retained_state()
    assert "original-private-authority" not in json.dumps(before)
    assert supervisor_smoke.retained_state() == before
    consumers["version"] = "refreshed-catalogue-after-restart"
    consumers["selected"] = list(reversed(original_consumers))
    assert supervisor_smoke.retained_state() == before
    consumers["selected"] = [{"entity_id": "sensor.dishwasher", "weather_sensitive": False}]
    assert supervisor_smoke.retained_state() != before
    consumers["selected"] = original_consumers
    history["5minute"][0]["sum"] = 76
    assert supervisor_smoke.retained_state() != before
    history["5minute"][0]["sum"] = 75
    history["hour"] = []
    assert supervisor_smoke.retained_state() != before
    history["hour"] = [{"start": 100, "sum": 50, "state": 1050}]
    assert supervisor_smoke.retained_state() == before
    token.write_bytes(b"replacement-private-authority")
    assert supervisor_smoke.retained_state() != before
    token.unlink()
    with pytest.raises(FileNotFoundError):
        supervisor_smoke.retained_state()


def test_pending_forecast_sqlite_error_keeps_bounded_message_and_original_cause(
    tmp_path, monkeypatch
):
    """A real missing archive table becomes a retryable bounded error with its SQLite cause retained."""
    directory = tmp_path / "apps/data" / supervisor_smoke.APP_SLUG
    directory.mkdir(parents=True)
    sqlite3.connect(directory / "archive.sqlite3").close()
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="Pending forecast captures are not ready") as caught:
        supervisor_smoke.pending_model_forecasts()
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert str(caught.value) == "Pending forecast captures are not ready."
    assert "no such table" not in str(caught.value)


@pytest.mark.parametrize("temporary_failure", ["missing_table", "locked"])
def test_wait_for_retries_actual_pending_forecast_sqlite_read(
    tmp_path, monkeypatch, temporary_failure
):
    """The existing poll retries an actual missing-table or locked read and accepts the repaired archive."""
    directory = tmp_path / "apps/data" / supervisor_smoke.APP_SLUG
    directory.mkdir(parents=True)
    connect = sqlite3.connect
    writer = connect(directory / "archive.sqlite3")
    if temporary_failure == "locked":
        writer.execute("CREATE TABLE oep_documents (namespace TEXT, name TEXT, payload TEXT)")
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
    expected = {"baseline": {"model": "weekday-hour-v2"}}
    failures, repairs = [], []

    def immediate_lock_check(*arguments, **keywords):
        """Expose the real SQLite lock immediately without changing production connection budgets."""
        return connect(*arguments, **keywords, timeout=0)

    def read_pending():
        """Observe retryable causes independently of the returned model document."""
        try:
            return supervisor_smoke.pending_model_forecasts()
        except RuntimeError as error:
            failures.append(type(error.__cause__))
            raise

    def repair_after_first_attempt(_seconds):
        """Publish the archive schema/row only after the first genuine failed read."""
        assert not repairs
        repairs.append(temporary_failure)
        if temporary_failure == "missing_table":
            writer.execute("CREATE TABLE oep_documents (namespace TEXT, name TEXT, payload TEXT)")
        writer.execute(
            "INSERT INTO oep_documents VALUES (?, ?, ?)",
            (
                "demand",
                "accuracy-pending-fixture.json",
                json.dumps({"capture": {"forecasts": expected}}),
            ),
        )
        writer.commit()

    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    monkeypatch.setattr(supervisor_smoke.sqlite3, "connect", immediate_lock_check)
    # The repair hook bounds retries independently of host scheduling delays.
    monkeypatch.setattr(provision_supervisor, "monotonic", lambda: 0.0)
    monkeypatch.setattr(provision_supervisor, "sleep", repair_after_first_attempt)
    try:
        actual = provision_supervisor.wait_for(read_pending, "retained pending captures", timeout=1)
    finally:
        writer.close()
    assert actual == [expected]
    assert failures == [sqlite3.OperationalError]
    assert repairs == [temporary_failure]
