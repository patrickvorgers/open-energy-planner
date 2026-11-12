"""Content-addressed public files cannot cache settings, traverse paths or mix software bytes."""

import asyncio
import re
import shutil

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from open_energy_planner.app import create_app
from open_energy_planner.frontend import assets


def test_asset_versions_freeze_bytes_and_leave_private_responses_uncached(tmp_path, monkeypatch):
    """Two app instances serve separate immutable assets while HTML, sessions and auth stay private."""
    public = tmp_path / "public"
    shutil.copytree(assets.PUBLIC, public)
    (public / "sample.js").write_text("export const value = 1;", encoding="utf-8")
    secret = tmp_path / "private.txt"
    secret.write_text("private-test-sentinel", encoding="utf-8")
    (public / "escape.txt").symlink_to(secret)
    monkeypatch.setattr(assets, "PUBLIC", public)
    token = "asset-version-test-0123456789"

    async def scenario():
        """Read actual generated URLs and retain the first instance across a source edit."""
        async with TestClient(TestServer(create_app(tmp_path / "first", token))) as first:
            html = await first.get("/workspace?page=%2Fsetup")
            assert html.headers["Cache-Control"] == "no-store"
            prefix = re.search(r"/setup/static/[0-9a-f]{64}/", await html.text()).group()
            response = await first.get(prefix + "sample.js")
            assert await response.text() == "export const value = 1;"
            assert response.content_type == "text/javascript"
            assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
            head = await first.head(prefix + "sample.js")
            assert head.status == 200 and await head.read() == b""
            for path in (
                prefix + "escape.txt",
                prefix + "%2e%2e/private.txt",
                prefix + "missing.js",
            ):
                response = await first.get(URL(path, encoded=True))
                assert response.status in (403, 404)
                assert (
                    response.headers.get("Cache-Control") != "public, max-age=31536000, immutable"
                )
                assert "private-test-sentinel" not in await response.text()
            assert (await first.get("/setup/session")).headers["Cache-Control"] == "no-store"
            assert (await first.get("/v1/setup")).status == 401
            assert (await first.post(prefix + "sample.js")).status == 401
            (public / "sample.js").write_text("export const value = 2;", encoding="utf-8")
            assert await (await first.get(prefix + "sample.js")).text() == "export const value = 1;"
            async with TestClient(TestServer(create_app(tmp_path / "second", token))) as second:
                document = await (await second.get("/workspace")).text()
                updated = re.search(r"/setup/static/[0-9a-f]{64}/", document).group()
                assert updated != prefix
                assert (
                    await (await second.get(updated + "sample.js")).text()
                    == "export const value = 2;"
                )
                assert (await second.get(prefix + "sample.js")).status == 404
                assert (await first.get(updated + "sample.js")).status == 404

    asyncio.run(scenario())
