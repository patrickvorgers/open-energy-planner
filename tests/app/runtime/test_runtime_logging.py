"""Verify timestamped application lifecycle evidence and actual graceful process termination."""

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest
from aiohttp import ClientResponseError, web
from aiohttp.test_utils import TestServer

from open_energy_planner.app import create_app
from open_energy_planner.planning import PlanningCoordinator
from open_energy_planner.runtime.diagnostics.logging import (
    BackgroundLog,
    UTCFormatter,
    register_lifecycle,
    request_error_message,
)


@pytest.mark.parametrize("payload", ["Safe rejection. SECRET", ["SECRET"], {"token": "SECRET"}])
def test_request_failure_requires_an_exact_owned_message(payload, caplog):
    """Similar text and non-string exception arguments never become public or logged payloads."""
    logger = logging.getLogger("open_energy_planner.request_test")
    try:
        raise ValueError(payload)
    except ValueError as error:
        result = request_error_message(
            error,
            logger=logger,
            operation="Settings save",
            safe_messages=("Safe rejection.",),
            fallback="Refresh settings before retrying.",
        )
    assert result == "Refresh settings before retrying."
    assert "SECRET" not in caplog.text
    assert "ValueError" in caplog.text
    assert "test_request_failure_requires_an_exact_owned_message" in caplog.text


def test_malformed_json_is_not_logged_as_an_application_defect(caplog):
    """Invalid user JSON gets owned advice without noisy ERROR logs or echoing private input."""
    logger = logging.getLogger("open_energy_planner.request_test")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        try:
            json.loads("SECRET invalid JSON")
        except json.JSONDecodeError as error:
            result = request_error_message(
                error,
                logger=logger,
                operation="Settings save",
                fallback="Check the settings request.",
            )
    assert result == "Check the settings request."
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert "SECRET" not in caplog.text


def test_lifecycle_messages_bracket_resources_and_graceful_stop(caplog):
    """Startup completes before started. Stopping precedes resource draining and stopped follows it."""

    async def scenario():
        """Use an actual server lifecycle and a resource context with observable cleanup."""
        app = web.Application()

        async def resource(_app):
            """Represent an owned background resource whose shutdown must finish before stopped."""
            logging.getLogger("open_energy_planner.resource").info("resource ready")
            yield
            logging.getLogger("open_energy_planner.resource").info("resource drained")

        app.cleanup_ctx.append(resource)
        register_lifecycle(app, "test-release", "synthetic")
        async with TestServer(app):
            assert caplog.messages[-1] == "Open Energy Planner started"
        assert caplog.messages == [
            "Open Energy Planner starting (release=test-release, source=synthetic)",
            "resource ready",
            "Open Energy Planner started",
            "Open Energy Planner stopping",
            "resource drained",
            "Open Energy Planner stopped",
        ]

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())


def test_failed_initialization_does_not_report_started(caplog):
    """A resource initialization failure emits starting and completed cleanup, never a false successful start."""

    async def scenario():
        """Fail during real aiohttp startup and explicitly drain initialized contexts."""
        app = web.Application()

        async def failure(_app):
            """Represent a failing resource initializer before it yields ownership."""
            raise RuntimeError("test initialization failure")
            yield

        app.cleanup_ctx.append(failure)
        register_lifecycle(app, "test-release", "synthetic")
        app.freeze()
        with pytest.raises(RuntimeError, match="initialization failure"):
            await app.startup()
        await app.cleanup()
        assert caplog.messages == [
            "Open Energy Planner starting (release=test-release, source=synthetic)",
            "Open Energy Planner stopped",
        ]

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())


def test_app_stopped_waits_for_planner_cleanup(tmp_path, monkeypatch, caplog):
    """Hold actual app planner cleanup. Stopped must appear only after the planner is fully drained."""

    async def scenario():
        """Exercise create_app's real cleanup registration with an explicitly blocked close."""
        closing, release = asyncio.Event(), asyncio.Event()
        original_close = PlanningCoordinator.close

        async def close(coordinator):
            """Represent an in-flight round that cannot finish before the release event."""
            closing.set()
            await release.wait()
            await original_close(coordinator)
            logging.getLogger("open_energy_planner.planning").info("planner drained")

        monkeypatch.setattr(PlanningCoordinator, "close", close)
        app = create_app(tmp_path, "test-token-0123456789abcdef", source="home_assistant")
        runner = web.AppRunner(app)
        await runner.setup()
        cleanup = asyncio.create_task(runner.cleanup())
        try:
            await asyncio.wait_for(closing.wait(), 5)
            assert "Open Energy Planner stopped" not in caplog.messages
        finally:
            release.set()
            await cleanup
        assert caplog.messages[-2:] == ["planner drained", "Open Energy Planner stopped"]

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())


def test_log_timestamp_is_explicit_utc_with_milliseconds():
    """An exact epoch is rendered as UTC even when the host local time zone differs."""
    record = logging.LogRecord("test", logging.INFO, "", 0, "started", (), None)
    record.created, record.msecs = 0, 123
    formatter = UTCFormatter("%(asctime)s.%(msecs)03dZ %(message)s", "%Y-%m-%dT%H:%M:%S")
    assert formatter.format(record) == "1970-01-01T00:00:00.123Z started"


def test_multiline_exception_and_stack_keep_uniform_prefix_and_shared_record():
    """Prefix all physical lines including chained exceptions, blank lines and stacks. Retain shared record contents."""
    try:
        try:
            raise ValueError("first cause")
        except ValueError as cause:
            raise OSError("storage\nfailure") from cause
    except OSError:
        record = logging.LogRecord(
            "open_energy_planner.test",
            logging.ERROR,
            "",
            0,
            "Could not save %s\nRetry later",
            ("plan",),
            sys.exc_info(),
            sinfo="Stack (most recent call last):\n  test frame",
        )
    record.created, record.msecs = 0, 123
    original = record.__dict__.copy()
    formatter = UTCFormatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s [%(name)s] %(message)s", "%Y-%m-%dT%H:%M:%S"
    )
    output = formatter.format(record)
    prefix = "1970-01-01T00:00:00.123Z ERROR [open_energy_planner.test] "
    assert all(line.startswith(prefix) for line in output.splitlines())
    payload = "\n".join(line.removeprefix(prefix) for line in output.splitlines())
    assert "Could not save plan\nRetry later" in payload
    assert "ValueError: first cause" in payload
    assert "OSError: storage\nfailure" in payload
    assert "direct cause" in payload
    assert payload.endswith("Stack (most recent call last):\n  test frame")
    assert record.__dict__ == original
    assert formatter.format(record) == output
    assert logging.Formatter("%(message)s").format(record) == payload


def test_empty_message_is_supported():
    """An empty message with a message-only format remains a valid empty diagnostic."""
    record = logging.LogRecord("test", logging.INFO, "", 0, "", (), None)
    assert UTCFormatter("%(message)s").format(record) == ""


def test_expected_background_failure_retry_recovery_levels_and_privacy(caplog):
    """One outage warns once, repeated attempts stay debug, recovery is visible and URLs never escape."""
    logger = logging.getLogger("open_energy_planner.background_test")
    status = BackgroundLog(logger, "Solar weather refresh")
    error = ClientResponseError(
        SimpleNamespace(real_url="https://weather.invalid/?latitude=PRIVATE_SITE&token=SECRET"),
        (),
        status=503,
        message="SECRET provider body",
    )
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        for _ in range(2):
            try:
                raise error
            except ClientResponseError:
                status.exception(
                    "Valid forecasts retain their original expiry.", retry_seconds=1800
                )
        status.success("Fresh weather is available.")
        status.success("Fresh weather is available.")
    assert [record.levelno for record in caplog.records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.INFO,
        logging.DEBUG,
    ]
    assert "HTTP 503" in caplog.messages[0]
    assert "Retrying in 30 minutes." in caplog.messages[0]
    assert "recovered" in caplog.messages[2]
    assert all(record.exc_info is None for record in caplog.records)
    assert "SECRET" not in caplog.text and "PRIVATE_SITE" not in caplog.text


def test_unexpected_background_defects_keep_tracebacks_without_poll_spam(caplog):
    """Unexpected software failures retain stack evidence and repeats move below the normal log level."""
    logger = logging.getLogger("open_energy_planner.background_test")
    status = BackgroundLog(logger, "Forecast update")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        for _ in range(2):
            try:
                raise RuntimeError("controlled defect")
            except RuntimeError:
                status.exception("The previous valid forecast is retained.", retry_seconds=5)
    assert [record.levelno for record in caplog.records] == [logging.ERROR, logging.DEBUG]
    assert all(record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records)
    assert "Retrying in 5 seconds." in caplog.messages[0]


def test_expected_waiting_is_informational_and_build_identity_is_logged(monkeypatch, caplog):
    """Normal waiting is not a warning, and numbered startup reports the actual packaged identity."""
    from open_energy_planner.build_info import BUILD_INFO

    monkeypatch.setitem(BUILD_INFO, "build", "999.2")
    monkeypatch.setitem(BUILD_INFO, "revision", "test-revision")

    async def scenario():
        """Start and close a real app lifecycle while recording immutable build metadata."""
        app = web.Application()
        register_lifecycle(app, "test-release", "synthetic")
        async with TestServer(app):
            pass

    logger = logging.getLogger("open_energy_planner.background_test")
    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        try:
            raise ValueError("prices not final")
        except ValueError:
            BackgroundLog(logger, "Published market prices").exception(
                "Waiting for the provider's complete delivery day.",
                retry_seconds=1800,
                expected=True,
                reason="prices not final",
                waiting=True,
            )
        asyncio.run(scenario())
    assert all(record.levelno == logging.INFO for record in caplog.records)
    assert "pending" in caplog.messages[0]
    assert "Application build: 999.2. Source revision: test-revision." in caplog.messages


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows terminate is a hard kill, not POSIX SIGTERM"
)
def test_app_sigterm_writes_timestamped_start_and_stop(tmp_path):
    """A real container-style process serves a request, handles SIGTERM and flushes all four UTC lifecycle messages."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {**os.environ, "OEP_API_TOKEN": "lifecycle-test-only-token-0123456789"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_energy_planner.app",
            "--demo",
            "--data",
            str(tmp_path),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    try:
        request = Request(
            f"http://127.0.0.1:{port}/v1/snapshot",
            headers={"Authorization": "Bearer " + environment["OEP_API_TOKEN"]},
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                with urlopen(request, timeout=1) as response:
                    assert response.status == 200
                break
            except URLError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Application did not start its HTTP server")
                time.sleep(0.05)
        process.terminate()
        output, _ = process.communicate(timeout=30)
        assert process.returncode == 0, output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    events = re.findall(
        "^(\\S+) INFO \\[open_energy_planner.runtime.diagnostics.logging\\] Open Energy Planner (starting|started|stopping|stopped)",
        output,
        re.MULTILINE,
    )
    assert [event for _, event in events] == ["starting", "started", "stopping", "stopped"], output
    times = [datetime.fromisoformat(stamp) for stamp, _ in events]
    assert all(value.tzinfo == UTC for value in times)
    assert times == sorted(times)
    assert environment["OEP_API_TOKEN"] not in output
    assert "GET /v1/snapshot" not in output
