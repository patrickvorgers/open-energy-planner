"""Verify convention-based public routing without exposing API or internal files."""

import asyncio
import shutil
from xml.etree import ElementTree

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from open_energy_planner.app import create_app
from open_energy_planner.frontend import assets


def test_new_public_files_and_pages_need_no_allowlist_and_keep_private_boundaries(
    tmp_path, monkeypatch
):
    """New assets inherit GET/HEAD access. Traversal, symlinks, partials and API writes remain protected."""
    pages = tmp_path / "pages"
    public = tmp_path / "public"
    shutil.copytree(assets.PAGES, pages)
    shutil.copytree(assets.PUBLIC, public)
    (pages / "future.html").write_text(
        "<html><body><p>Future page</p></body></html>", encoding="utf-8"
    )
    (public / "future.js").write_text("export const future = true;", encoding="utf-8")
    private = tmp_path / "private.txt"
    private.write_text("private installation secret", encoding="utf-8")
    (public / "escape.txt").symlink_to(private)
    (pages / "escape.html").symlink_to(private)
    monkeypatch.setattr(assets, "PAGES", pages)
    monkeypatch.setattr(assets, "PUBLIC", public)
    token = "public-routing-test-0123456789"

    async def scenario():
        """Exercise the real router, static resolver, middleware and response preparation."""
        async with TestClient(TestServer(create_app(tmp_path / "data", token))) as client:
            response = await client.get("/setup/future.js")
            assert response.status == 200
            assert await response.text() == "export const future = true;"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "object-src 'none'" in response.headers["Content-Security-Policy"]
            response = await client.head("/setup/future.js")
            assert response.status == 200 and await response.read() == b""
            response = await client.get("/future")
            assert response.status == 200 and response.content_type == "text/html"
            document = ElementTree.fromstring(await response.text())
            assert document.find("body/p").text == "Future page"
            response = await client.get("/setup/session")
            assert response.status == 200 and response.content_type == "application/json"
            assert "build_info" in await response.json()
            assert (await client.get("/v1/setup")).status == 401
            assert (await client.post("/setup/future.js")).status == 401
            assert (await client.post("/setup/session")).status == 401
            assert (await client.post("/future")).status == 401
            for path in (
                "/setup/escape.txt",
                "/setup/%2e%2e/private.txt",
                "/setup/../partials/navigation.html",
                "/setup/navigation.html",
                "/setup/icons/",
                "/escape",
            ):
                response = await client.get(URL(path, encoded=True))
                assert response.status in {401, 403, 404}
                assert "private installation secret" not in await response.text()

    asyncio.run(scenario())
