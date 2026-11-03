"""Loaded identity, peer mismatch and deployment proof independent of checkout state."""

import asyncio

import pytest
from aiohttp import ClientSession, web

from custom_components.open_energy_planner.client import LocalClient, ReleaseMismatchError
from open_energy_planner.app import create_app
from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS
from script.release import package_release
from script.release.verify_deployment import validate_url, verify
from tests.ha_contract.support.connection import TOKEN, server


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8104",
        "http://127.0.0.1:8099",
        "http://[::1]:8099",
        "https://ha.example/api/hassio_ingress/test",
    ],
)
def test_deployment_url_permits_local_http_or_verified_https(url):
    """Local checks and HTTPS ingress are accepted without weakening TLS verification."""
    validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://haos.example.test:8123",
        "http://example.test",
        "file:///etc/passwd",
        "https://user:password@example.test",
    ],
)
def test_deployment_url_rejects_cleartext_remote_and_embedded_credentials(url):
    """The checker rejects unsafe destinations before constructing an authenticated request."""
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("failure", ["redirect", "oversized"])
def test_deployment_http_keeps_token_at_target_and_bounds_responses(tmp_path, monkeypatch, failure):
    """The deployment CLI rejects redirects and oversized version metadata before accepting any identity."""
    from urllib.error import HTTPError

    from script.release import verify_deployment

    async def scenario():
        """Use real local HTTP and assert that a redirect destination is never contacted."""
        redirected = []

        async def version(request):
            """Return a redirect or independently sized one-MiB response after checking the intended token."""
            assert request.headers["Authorization"] == "Bearer " + TOKEN
            if failure == "redirect":
                raise web.HTTPFound("/unexpected")
            return web.json_response({"padding": "x" * 1048576})

        async def unexpected(request):
            """Record any forbidden redirect follow-up without exposing its headers."""
            redirected.append(True)
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/v1/version", version)
        app.router.add_get("/unexpected", unexpected)
        async with server(app) as url:
            monkeypatch.setenv("OEP_API_TOKEN", TOKEN)
            monkeypatch.setattr(
                "sys.argv", ["verify_deployment", url, str(tmp_path / "unused.json")]
            )
            with pytest.raises(
                HTTPError if failure == "redirect" else ValueError,
                match="302" if failure == "redirect" else "size limit",
            ):
                await asyncio.to_thread(verify_deployment.main)
            assert not redirected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tag,version",
    [
        ("v0.3.0", "0.3.0"),
        ("1.2.3", "1.2.3"),
        ("v1.0.0-rc.1", "1.0.0-rc.1"),
        ("1.2.3-" + "a" * 122, "1.2.3-" + "a" * 122),
        ("v1.2.3-" + "a" * 122, "1.2.3-" + "a" * 122),
    ],
)
def test_release_tag_determines_version(tag, version):
    """A published tag supplies the release version without incrementing or guessing from its title."""
    assert package_release.version_from_tag(tag) == version


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "latest",
        "v1.2",
        "v01.2.3",
        "v1.2.3-01",
        "v1.2.3/other",
        "v1.2.3+build.1",
        "1.2.3-" + "a" * 123,
    ],
)
def test_invalid_release_tag_is_rejected(tag):
    """Malformed tags cannot become app or integration versions."""
    with pytest.raises(ValueError, match="semantic version"):
        package_release.version_from_tag(tag)


def test_release_tag_must_identify_the_built_commit(tmp_path, monkeypatch):
    """A valid release number cannot stamp a checkout from a different tag target."""

    def git_output(arguments, **kwargs):
        """Provide a clean checkout with a release tag pointing to another commit."""
        if arguments[1] == "status":
            return ""
        if arguments[-1] == "HEAD":
            return "a" * 40
        assert arguments[-1] == "refs/tags/v0.3.0^{commit}"
        return "b" * 40

    monkeypatch.setattr(package_release.subprocess, "check_output", git_output)
    with pytest.raises(ValueError, match="tag must identify"):
        package_release.build(tmp_path / "bundle", release_tag="v0.3.0", build_number="142.2")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("field", ["version", "build"])
def test_app_rejects_mixed_identity_before_mutation(tmp_path, field):
    """A different version or build rejects enable writes and remains visible to the administrator."""

    async def scenario():
        """Exercise the real authenticated middleware and verify that rejected writes cannot disable planning."""
        app = create_app(tmp_path, TOKEN, source="home_assistant")
        peer = {**BUILD_INFO, field: "different"}
        headers = {
            "Authorization": "Bearer " + TOKEN,
            **{header: peer[key] or "" for key, header in HEADER_FIELDS.items()},
        }
        async with server(app) as url, ClientSession() as session:
            async with session.put(
                url + "/v1/enabled", json={"enabled": False}, headers=headers
            ) as response:
                assert response.status == 409
            async with session.get(
                url + "/v1/version", headers={"Authorization": "Bearer " + TOKEN}
            ) as response:
                status = await response.json()
                assert status["status"] == "mismatch"
                assert status["integration"] == {key: peer[key] for key in HEADER_FIELDS}
            async with session.get(
                url + "/v1/snapshot", headers={"Authorization": "Bearer " + TOKEN}
            ) as response:
                assert (await response.json())["enabled"] is True

    asyncio.run(scenario())


def test_loaded_peer_and_app_identity_match_and_missing_peer_is_explicit(tmp_path):
    """Only a successful request from the loaded integration produces matched process provenance."""

    async def scenario():
        """Use the real integration HTTP client rather than merely inspecting installed files."""
        async with (
            server(create_app(tmp_path, TOKEN, source="home_assistant")) as url,
            ClientSession() as session,
        ):
            async with session.get(url + "/setup/session") as response:
                assert (await response.json())["build_info"]["status"] == "unavailable"
            client = LocalClient(session, url, TOKEN)
            assert (await client.snapshot())["release"] == BUILD_INFO["version"]
            async with session.get(
                url + "/v1/version", headers={"Authorization": "Bearer " + TOKEN}
            ) as response:
                status = await response.json()
                assert status["status"] == "matched"
                assert status["app"] == BUILD_INFO
                assert status["integration"] == {key: BUILD_INFO[key] for key in HEADER_FIELDS}

    asyncio.run(scenario())


def test_peer_contact_expires_and_unauthenticated_spoof_cannot_update_it(tmp_path, monkeypatch):
    """A stale integration cannot remain matched, and unauthenticated callers cannot overwrite observed identity."""
    from open_energy_planner import build_info

    async def scenario():
        """Invalidate the contact deadline without sleeping or altering the process clock."""
        async with (
            server(create_app(tmp_path, TOKEN, source="home_assistant")) as url,
            ClientSession() as session,
        ):
            await LocalClient(session, url, TOKEN).snapshot()
            async with session.get(
                url + "/v1/version", headers={"X-OEP-Release": "spoof"}
            ) as response:
                assert response.status == 401
            monkeypatch.setattr(build_info, "INTEGRATION_CONTACT_MAX_AGE_SECONDS", -1)
            async with session.get(url + "/setup/session") as response:
                status = (await response.json())["build_info"]
                assert status["status"] == "unavailable"
                assert status["integration"] == {key: BUILD_INFO[key] for key in HEADER_FIELDS}

    asyncio.run(scenario())


@pytest.mark.parametrize("field", [None, "version", "build"])
def test_integration_rejects_unidentified_or_different_app(field):
    """An old or falsely matching app cannot return commands without identifying the loaded build."""

    async def scenario():
        """Keep the JSON release equal so only the independent response identity can reject it."""
        headers = (
            {header: BUILD_INFO[key] or "" for key, header in HEADER_FIELDS.items()}
            if field
            else {}
        )
        if field:
            headers[HEADER_FIELDS[field]] = "different"
        app = web.Application()

        async def snapshot(request):
            """Return a plausible release with absent or different process provenance."""
            return web.json_response({"release": BUILD_INFO["version"]}, headers=headers)

        app.router.add_get("/v1/snapshot", snapshot)
        async with server(app) as url, ClientSession() as session:
            with pytest.raises(ReleaseMismatchError):
                await LocalClient(session, url, TOKEN).snapshot()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["unavailable", "mismatch"])
def test_deployment_checker_requires_expected_build_not_only_pair_equality(status):
    """Reject stale or mismatched contact independently from incorrect loaded identities."""
    expected = {"version": "0.2.0", "build": "142.2", "revision": "a" * 40}
    manifest = {**expected, "source_state": "commit"}
    verify({"status": "matched", "app": expected, "integration": expected}, manifest)
    with pytest.raises(ValueError):
        verify({"status": status, "app": expected, "integration": expected}, manifest)
    with pytest.raises(ValueError):
        verify({"status": "matched", "app": BUILD_INFO, "integration": BUILD_INFO}, manifest)


def test_deployment_checker_rejects_unnumbered_sources_but_ignores_diagnostic_commit():
    """Development sources cannot masquerade as a CI build, while matching numbered builds ignore diagnostic revisions."""
    with pytest.raises(ValueError, match="numbered CI build"):
        verify(
            {"status": "matched", "app": BUILD_INFO, "integration": BUILD_INFO},
            {**BUILD_INFO, "source_state": "commit"},
        )
    expected = {
        "version": "0.2.0",
        "build": "142.2",
        "revision": "a" * 40,
        "source_state": "commit",
    }
    loaded = {"version": "0.2.0", "build": "142.2", "revision": "b" * 40}
    verify({"status": "matched", "app": loaded, "integration": loaded}, expected)


def test_commit_metadata_does_not_change_runtime_version_build_compatibility(tmp_path, monkeypatch):
    """The integration and app compare release/build only, even when diagnostic source revisions differ."""
    from custom_components.open_energy_planner import client as client_module

    async def scenario():
        """Use the real client and app with deliberately different internal commit metadata."""
        monkeypatch.setattr(client_module, "BUILD_INFO", {**BUILD_INFO, "revision": "b" * 40})
        async with (
            server(create_app(tmp_path, TOKEN, source="home_assistant")) as url,
            ClientSession() as session,
        ):
            assert (await LocalClient(session, url, TOKEN).snapshot())["release"] == BUILD_INFO[
                "version"
            ]
            async with session.get(url + "/setup/session") as response:
                assert (await response.json())["build_info"]["status"] == "matched"

    asyncio.run(scenario())


@pytest.mark.parametrize("build_number", ["", "1", "1.0", "-1.1", "1.2x", 12])
def test_bundle_rejects_invalid_build_before_writing(tmp_path, build_number):
    """Malformed run/attempt identities must not produce distributable artifacts."""
    with pytest.raises(ValueError, match="Build number"):
        package_release.build(tmp_path / "bundle", revision="a" * 40, build_number=build_number)
    assert not (tmp_path / "bundle").exists()
