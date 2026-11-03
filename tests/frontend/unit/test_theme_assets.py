"""Keep the complete local theme available without opening an arbitrary asset directory."""

import asyncio
import hashlib
import json
import struct
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from open_energy_planner.app import create_app

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "src/open_energy_planner/web/public"
ICON_NAMES = (
    "app",
    "planning",
    "balance",
    "savings",
    "consumers",
    "quality",
    "appliances",
    "contract",
    "settings",
    "ev",
    "solar",
    "home",
    "battery",
)


def test_theme_assets_are_local_packaged_and_public_but_not_a_directory_server(tmp_path):
    """All used and unused symbols and licensed fonts work without an API token or CDN."""

    async def scenario():
        """Exercise the public static root and reject missing or escaping file paths."""
        async with TestClient(
            TestServer(create_app(tmp_path, "theme-test-token-0123456789", source="home_assistant"))
        ) as client:
            for name in ICON_NAMES:
                response = await client.get(f"/setup/icons/{name}.svg")
                assert response.status == 200
                assert response.content_type == "image/svg+xml"
                assert await response.read() == (WEB / "icons" / f"{name}.svg").read_bytes()
            for source in json.loads((WEB / "fonts/sources.json").read_text()):
                response = await client.get(f"/setup/fonts/{source['file']}")
                assert response.status == 200
                assert hashlib.sha256(await response.read()).hexdigest() == source["sha256"]
            for license_name in ("outfit-OFL.txt", "spacegrotesk-OFL.txt"):
                response = await client.get(f"/setup/fonts/{license_name}")
                assert response.status == 200
                assert "SIL OPEN FONT LICENSE" in await response.text()
            for path in (
                "/setup/icons/unknown.svg",
                "/setup/fonts/unknown.ttf",
            ):
                assert (await client.get(path)).status in {403, 404}
                response = await client.get(
                    path, headers={"Authorization": "Bearer theme-test-token-0123456789"}
                )
                assert response.status == 404

            traversal_path = "/setup/icons/%2e%2e/%2e%2e/%2e%2e/app.py"
            traversal_url = URL(traversal_path, encoded=True)
            target = (WEB / traversal_url.path.removeprefix("/setup/")).resolve()
            assert target.is_file()
            assert not target.is_relative_to(WEB.resolve())
            for headers, expected_statuses in (
                ({}, {401, 403, 404}),
                ({"Authorization": "Bearer theme-test-token-0123456789"}, {404}),
            ):
                response = await client.get(traversal_url, headers=headers)
                assert response.request_info.url.raw_path == traversal_path
                assert response.status in expected_statuses

    asyncio.run(scenario())
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ]["open_energy_planner"]
    for folder in (WEB / "icons", WEB / "fonts"):
        for asset in folder.iterdir():
            assert any(asset.relative_to(WEB.parent.parent).match(pattern) for pattern in patterns)


def test_icon_family_and_home_assistant_brand_keep_the_approved_identity():
    """Task SVGs retain the palette and the integration ships a transparent square PNG."""
    assert {asset.stem for asset in (WEB / "icons").glob("*.svg")} == set(ICON_NAMES)
    for name in ICON_NAMES:
        svg = ElementTree.fromstring((WEB / "icons" / f"{name}.svg").read_text())
        assert svg.attrib["viewBox"] == ("0 0 64 64" if name == "app" else "0 0 24 24")
        for element in svg.iter():
            for attribute in ("fill", "stroke"):
                if attribute in element.attrib:
                    assert element.attrib[attribute] in {"none", "#4387bf", "#21836d", "#f9d544"}
    image = (ROOT / "custom_components/open_energy_planner/brand/icon.png").read_bytes()
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _, color_type = struct.unpack(">IIBB", image[16:26])
    assert (width, height, color_type) == (512, 512, 6)


def test_packaged_pages_include_every_routed_directory_depth(tmp_path):
    """New nested HTML pages remain packaged at the same depths that the router discovers."""
    import glob
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = configuration["tool"]["setuptools"]["package-data"]["open_energy_planner"]
    pages = [tmp_path / "web/pages" / ("nested/" * depth) / "new.html" for depth in range(4)]
    for page in pages:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<title>New page</title>", encoding="utf-8")
    included = {
        Path(item)
        for pattern in patterns
        for item in glob.glob(str(tmp_path / pattern), recursive=True)
    }
    assert set(pages) <= included
