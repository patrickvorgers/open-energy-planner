"""Verify Supervisor-proof pairing, ingress socket-peer trust, app packaging and database options before privilege drop."""

import asyncio
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.pairing import Pairing
from tests.ha_contract.support.connection import PEER, SECRET, TOKEN, server


class StylesheetLinks(HTMLParser):
    """Read decoded stylesheet links from the actual ingress page response."""

    def __init__(self, html):
        """Collect only linked stylesheets, independently of HTML attribute order."""
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        """Retain stylesheet URLs without interpreting scripts or other page content."""
        values = dict(attributes)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.links.append(values["href"])


def test_supervisor_proof_reconnects_same_owner_without_consent(tmp_path):
    """Only the private Supervisor proof binds a peer. Retries retain identity and never replace another HA."""
    from open_energy_planner.pairing import administration_token

    async def scenario():
        """Exercise fresh pairing, restart and credential recovery without any approval call."""
        first = administration_token(tmp_path)
        assert len(first) >= 48 and administration_token(tmp_path) == first
        pairing = Pairing(tmp_path, "automatic-instance")
        with pytest.raises(web.HTTPForbidden):
            await pairing.supervisor_pair(PEER)
        pairing.supervisor_secret = SECRET
        with pytest.raises(web.HTTPForbidden):
            await pairing.supervisor_pair({**PEER, "secret": "wrong"})
        token = (await pairing.supervisor_pair(PEER))["token"]
        assert pairing.summary()["supervisor_managed"]
        assert not pairing.pending
        assert not pairing.authorized("Bearer \u00e9")
        assert not pairing.authorized("Bearer \udcff")
        assert (await pairing.supervisor_pair(PEER))["token"] == token
        with pytest.raises(web.HTTPConflict):
            await pairing.supervisor_pair({**PEER, "peer_id": "another-ha-instance"})
        restarted = Pairing(tmp_path, "automatic-instance")
        assert restarted.authorized("Bearer " + token)
        assert restarted.supervisor_secret is None
        restarted.supervisor_secret = "new-private-supervisor-proof"
        assert (await restarted.supervisor_pair({**PEER, "secret": restarted.supervisor_secret}))[
            "token"
        ] == token
        await restarted.reset()
        recovered = await restarted.supervisor_pair({**PEER, "secret": restarted.supervisor_secret})
        assert recovered["token"] != token
        assert restarted.summary()["supervisor_managed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("trusted", [False, True])
def test_ingress_trust_requires_socket_peer_and_valid_prefix(tmp_path, trusted, monkeypatch):
    """HA ingress authenticates from its actual proxy address. A forged forwarded address never grants access."""
    from open_energy_planner.build_info import BUILD_INFO

    monkeypatch.setitem(BUILD_INFO, "revision", "a" * 40)
    monkeypatch.setitem(BUILD_INFO, "release_tag", "v1.2.3")

    async def scenario():
        """Check anonymous session discovery, API protection and prefix-safe assets without leaking credentials."""
        app = create_app(
            tmp_path,
            TOKEN,
            source="home_assistant",
            ingress_peer="127.0.0.1" if trusted else "172.30.32.2",
        )
        async with server(app) as url, ClientSession() as client:
            headers = {
                "X-Ingress-Path": "/api/hassio_ingress/test_123",
                "X-Forwarded-For": "172.30.32.2",
            }
            async with client.get(url + "/setup/session", headers=headers) as response:
                session = await response.json()
                assert session["ingress"] is trusted and session["timezone"] is None
                assert session["build_info"]["status"] == "unavailable"
                assert session["build_info"]["app"] == {
                    "version": BUILD_INFO["version"],
                    "build": BUILD_INFO["build"],
                }
                assert "a" * 40 not in str(session)
                assert "v1.2.3" not in str(session)
                assert TOKEN not in str(session)
            async with client.get(url + "/v1/snapshot", headers=headers) as response:
                assert response.status == (200 if trusted else 401)
            async with client.get(url + "/setup", headers=headers) as response:
                html = await response.text()
                assert TOKEN not in html
                stylesheets = StylesheetLinks(html).links
                expected_names = {
                    "style.css",
                    "workspace.css",
                    "settings-layout.css",
                    "installation-layout.css",
                }
                assert len(stylesheets) == len(expected_names)
                assert {link.rsplit("/", 1)[-1] for link in stylesheets} == expected_names
                prefix = "/api/hassio_ingress/test_123" if trusted else ""
                for link in stylesheets:
                    assert link.startswith(prefix + "/setup/static/")
                    # Home Assistant strips its ingress prefix before proxying the request.
                    asset_path = link.removeprefix(prefix)
                    assert re.fullmatch(r"/setup/static/[0-9a-f]{64}/[^/]+\.css", asset_path)
                    async with client.get(url + asset_path, headers=headers) as asset:
                        assert asset.status == 200
                        assert asset.content_type == "text/css"
                        assert asset.headers["X-Content-Type-Options"] == "nosniff"
                        assert asset.headers["Referrer-Policy"] == "no-referrer"
                        assert "script-src 'self'" in asset.headers["Content-Security-Policy"]
                        expected = (
                            Path(__file__).resolve().parents[3]
                            / "src/open_energy_planner/web/public"
                            / link.rsplit("/", 1)[-1]
                        )
                        assert await asset.read() == expected.read_bytes()
            for value in ("https://evil.example", "/api/hassio_ingress/test/../../evil", ""):
                async with client.get(
                    url + "/v1/snapshot", headers={"X-Ingress-Path": value}
                ) as response:
                    assert response.status == 401
            async with client.get(
                url + "/v1/snapshot", headers={"Authorization": "Bearer \u00e9"}
            ) as response:
                assert response.status == 401
            from urllib.parse import urlsplit

            reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(url).port)
            try:
                writer.write(
                    b"GET /v1/snapshot HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer \xff\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                assert (await reader.readline()).startswith(b"HTTP/1.1 401")
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


def test_ha_app_package_enables_trusted_bootstrap_without_user_settings(tmp_path, monkeypatch):
    """The distribution build enables Supervisor/ingress and excludes browser tokens, published admin ports and lab credentials."""

    from script.release import package_ha_app
    from script.release.package_ha_app import package

    monkeypatch.setattr(package_ha_app.subprocess, "check_output", lambda *args, **kwargs: "")

    output = package(tmp_path / "repository")
    config = json.loads((output / "config.yaml").read_text())
    assert config["homeassistant_api"] and config["hassio_api"] and config["ingress"]
    assert config["hassio_role"] == "default"
    assert config["ingress_port"] == 8099
    assert config["options"] == {}
    assert config["schema"] == {
        "database_url": "password?",
        "migrate_database": "bool?",
        "recover_storage": "bool?",
    }
    assert config["discovery"] == ["open_energy_planner"]
    assert "ports" not in config and "map" not in config
    assert (output / "src/open_energy_planner/supervisor/__init__.py").is_file()
    assert not (output / ".local").exists()
    assert {path.name for path in (output / "script").iterdir()} == {"ha_app.py"}
    with pytest.raises(FileExistsError):
        package(tmp_path / "repository")


def test_supervisor_database_options_are_read_before_privilege_drop(tmp_path, monkeypatch):
    """The product HA app reads private settings while root, before any switch to UID 10001."""
    from importlib import import_module

    module = import_module("script.runtime.ha_app")
    events = []
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-only")
    monkeypatch.setattr(module, "Path", lambda _: tmp_path)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module.os, "chown", lambda *args: None, raising=False)
    monkeypatch.setattr(module.os, "setgroups", lambda *args: None, raising=False)
    monkeypatch.setattr(module.os, "setgid", lambda *args: None, raising=False)

    def read_options(data):
        """Record settings access before the simulated privilege boundary stops startup."""
        assert data == tmp_path
        events.append("read")

    def drop_uid(uid):
        """Stop before serving. Settings must already have been consumed as root."""
        assert uid == 10001 and events == ["read"]
        raise RuntimeError("test privilege boundary")

    monkeypatch.setattr(module, "configure_database_options", read_options)
    monkeypatch.setattr(module.os, "setuid", drop_uid, raising=False)
    with pytest.raises(RuntimeError, match="test privilege boundary"):
        module.main()


def test_supervisor_database_options_keep_credentials_private(tmp_path, monkeypatch):
    """Optional settings feed the runtime environment without exposing credentials in validation errors."""
    import os

    from script.runtime.ha_app import configure_database_options

    monkeypatch.setenv("OEP_DATABASE_URL", "")
    monkeypatch.setenv("OEP_MIGRATE_DATABASE", "")
    monkeypatch.setenv("OEP_RECOVER_STORAGE", "")
    configure_database_options(tmp_path)
    assert not os.environ["OEP_DATABASE_URL"]
    options = tmp_path / "options.json"
    url = "mysql+pymysql://test:private-password@mysql/archive"
    options.write_text(
        json.dumps({"database_url": url, "migrate_database": True, "recover_storage": True})
    )
    configure_database_options(tmp_path)
    assert os.environ["OEP_DATABASE_URL"] == url and os.environ["OEP_MIGRATE_DATABASE"] == "1"
    assert os.environ["OEP_RECOVER_STORAGE"] == "1"
    options.write_text(json.dumps({"database_url": {"private-password": "bad type"}}))
    with pytest.raises(TypeError, match="^Invalid database options$"):
        configure_database_options(tmp_path)
