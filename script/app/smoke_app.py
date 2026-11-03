"""Exercise the standalone app process, without HA installed or any published port."""

import asyncio
import json
import os
import sys
from tempfile import TemporaryDirectory

from aiohttp import ClientError, ClientSession


async def stop_process(process, *, timeout_s=5):
    """Reap exited children too, without replacing an earlier readiness failure."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # The child may exit between the returncode check and terminate.
    try:
        await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.communicate(), timeout=timeout_s)


async def check():
    """Exercise authenticated standalone app startup, pause/resume and fresh-plan acceptance."""
    token = "standalone-test-only-0123456789abcdef"
    with TemporaryDirectory() as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "open_energy_planner.app",
            "--demo",
            "--data",
            directory,
            env={**os.environ, "OEP_API_TOKEN": token},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with ClientSession() as session:
                url = "http://127.0.0.1:8099"
                for _ in range(100):
                    try:
                        async with session.get(url + "/v1/snapshot") as response:
                            assert response.status == 401
                        break
                    except ClientError:
                        await asyncio.sleep(0.05)
                else:
                    raise AssertionError("App did not become ready")
                headers = {"Authorization": f"Bearer {token}"}
                for _ in range(300):
                    async with session.get(url + "/v1/snapshot", headers=headers) as response:
                        initial = await response.json()
                    if initial.get("plan") is not None:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("No validated solver plan")
                assert initial["plan"]["independent_costs"]["objective"] < 0
                for enabled, request_id in ((False, "pause"), (True, "resume")):
                    async with session.put(
                        url + "/v1/enabled",
                        headers=headers,
                        json={
                            "enabled": enabled,
                            "request_id": request_id,
                        },
                    ) as response:
                        assert response.status == 200
                        latest = await response.json()
                        assert latest["enabled"] is enabled
                for _ in range(300):
                    async with session.get(url + "/v1/snapshot", headers=headers) as response:
                        latest = await response.json()
                    if latest["plan"]["plan_id"] != initial["plan"]["plan_id"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("No newly validated resume plan")
                print(
                    json.dumps(
                        {
                            "standalone_process": "passed",
                            "authentication": "passed",
                            "pause_resume": "passed",
                            "new_resume_plan": "passed",
                        }
                    )
                )
        finally:
            await stop_process(process)


if __name__ == "__main__":
    asyncio.run(check())
