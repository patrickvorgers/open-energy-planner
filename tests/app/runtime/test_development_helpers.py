"""Exercise reviewed helper paths independently of full application readiness."""

import asyncio
import io
import json
import runpy
import sys
from pathlib import Path

import pytest

from script.app.smoke_app import stop_process

ROOT = Path(__file__).resolve().parents[3]


def test_live_container_is_healthy_while_waiting_for_first_source_configuration(monkeypatch):
    """Start live setup without sources. Accept container health while scripted demo mode still requires a plan."""
    monkeypatch.setenv("OEP_API_TOKEN", "isolated-health-fixture-token")
    state = {"source": "home_assistant", "plan": None, "planning": {"status": "not_ready"}}
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(state).encode())
    )
    runpy.run_path(str(ROOT / "script/runtime/healthcheck.py"), run_name="__main__")
    # The demo still waits for a validated plan before provisioning its synthetic chain.
    state["source"] = "synthetic"
    with pytest.raises(SystemExit, match="No validated plan"):
        runpy.run_path(str(ROOT / "script/runtime/healthcheck.py"), run_name="__main__")


def test_notice_header_is_a_string_not_a_tuple(tmp_path, monkeypatch):
    """Generate runtime notices with no distributions. Require plain Markdown and the separate-HA notice."""
    output = tmp_path / "notices.md"
    monkeypatch.setattr(sys, "argv", ["runtime_notices.py", str(output)])
    monkeypatch.setattr("importlib.metadata.distributions", list)
    runpy.run_path(str(ROOT / "script/release/runtime_notices.py"))
    assert output.read_text().startswith("# Standalone runtime third-party notices\n")
    assert "Home Assistant is a separate runtime" in output.read_text()


def test_cleanup_reaps_already_exited_actual_child_without_terminating():
    """Reap a real exited subprocess. Cleanup must complete without attempting unnecessary termination."""

    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await process.wait()
        assert process.returncode == 0
        await stop_process(process)

    asyncio.run(scenario())


def test_cleanup_handles_exit_between_returncode_check_and_termination():
    """Simulate a child exiting during termination. Tolerate ProcessLookupError and still reap its output."""

    class ExitingProcess:
        returncode = None
        reaped = False

        def terminate(self):
            self.returncode = 0
            raise ProcessLookupError

        async def communicate(self):
            self.reaped = True

    process = ExitingProcess()
    asyncio.run(stop_process(process))
    assert process.reaped


def test_cleanup_does_not_wait_forever_for_inherited_pipes():
    """Keep child pipes open after exit. Enforce bounded cleanup rather than hanging indefinitely."""

    class OpenPipeProcess:
        returncode = None
        calls = 0

        def terminate(self):
            self.returncode = 0

        async def communicate(self):
            self.calls += 1
            await asyncio.Event().wait()

    process = OpenPipeProcess()
    with pytest.raises(TimeoutError):
        asyncio.run(stop_process(process, timeout_s=0.001))
    assert process.calls == 2
