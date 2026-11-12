"""Exercise real HTTP rendering against forged ingress paths and hostile page fragments."""

import asyncio
from html.parser import HTMLParser

import pytest
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from open_energy_planner.app import create_app


class ParsedPage(HTMLParser):
    """Inspect the resulting elements and decoded attributes instead of source-code literals."""

    def __init__(self, html):
        """Parse one actual HTTP response with standard HTML attribute decoding."""
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        """Retain each start tag so injected markup and event attributes remain observable."""
        self.elements.append((tag, dict(attributes)))


@pytest.mark.parametrize("shell_route", ["/", "/workspace"])
@pytest.mark.parametrize("ingress", [False, True])
@pytest.mark.parametrize(
    ("selection", "content_route"),
    [
        ("/setup/#installation", "/setup"),
        ("/setup#installation", "/setup"),
        ("/consumers#history", "/quality"),
        ("/quality#history", "/quality"),
        ("/overview#savings", "/overview"),
        ("/setup/../private#ignored", "/setup"),
    ],
)
def test_workspace_initial_frame_fetches_a_canonical_uncached_page(
    tmp_path, shell_route, ingress, selection, content_route
):
    """Root and ingress shells load real HTML for aliases and canonical views while rejecting invalid paths."""

    async def scenario():
        """Fetch the generated frame address through the same prefix stripping as the ingress proxy."""
        prefix = "/api/hassio_ingress/synthetic_123" if ingress else ""
        headers = {"X-Ingress-Path": prefix} if ingress else {}
        app = create_app(
            tmp_path,
            "synthetic-rendering-token-0123456789",
            source="home_assistant",
            ingress_peer="127.0.0.1" if ingress else None,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get(shell_route, params={"page": selection}, headers=headers)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            page = ParsedPage(await response.text())
            frames = [attributes for tag, attributes in page.elements if tag == "iframe"]
            assert len(frames) == 1
            frame_url = URL(frames[0]["src"])
            # The browser sends no fragment and the trusted proxy removes only its own prefix.
            child = await client.get(
                str(frame_url.with_fragment(None))[len(prefix) :], headers=headers
            )
            assert child.status == 200
            assert child.content_type == "text/html"
            assert child.headers["Cache-Control"] == "no-store"
            assert frame_url.path == prefix + content_route
            assert frame_url.query == {"embedded": "1"}
            assert frame_url.fragment == (selection.split("#")[1] if ".." not in selection else "")
            assert (await client.get("/v1/setup", headers=headers)).status == (
                200 if ingress else 401
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("trusted_peer", [False, True])
@pytest.mark.parametrize(
    ("ingress_path", "accepted_prefix"),
    [
        ("/api/hassio_ingress/synthetic_123", "/api/hassio_ingress/synthetic_123"),
        ('/api/hassio_ingress/synthetic" onload="alert(1)', ""),
        ('/api/hassio_ingress/synthetic"><script>alert(1)</script>', ""),
        ("//example.invalid/api/hassio_ingress/synthetic", ""),
    ],
)
def test_ingress_and_workspace_values_cannot_create_html_elements(
    tmp_path, trusted_peer, ingress_path, accepted_prefix
):
    """Only a trusted, valid prefix rewrites links. Hostile fragments stay inside one decoded attribute."""

    async def scenario():
        """Send attacker-controlled header/query values through the actual middleware and renderer."""
        app = create_app(
            tmp_path,
            "synthetic-rendering-token-0123456789",
            source="home_assistant",
            ingress_peer="127.0.0.1" if trusted_peer else None,
        )
        prefix = accepted_prefix if trusted_peer else ""
        fragment = '"><svg onload="alert(1)"><script>alert(1)</script>'
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Ingress-Path": ingress_path}
            for route, parameters in (
                ("/planning-performance", {}),
                ("/workspace", {"page": "/planning-performance#" + fragment}),
            ):
                response = await client.get(route, headers=headers, params=parameters)
                assert response.status == 200
                policy = response.headers["Content-Security-Policy"]
                assert "script-src 'self'" in policy and "base-uri 'none'" in policy
                page = ParsedPage(await response.text())
                assert not any(
                    attribute.startswith("on")
                    for _tag, attributes in page.elements
                    for attribute in attributes
                )
                scripts = [attributes for tag, attributes in page.elements if tag == "script"]
                assert scripts and all(
                    attributes.get("src", "").startswith(prefix + "/setup/")
                    for attributes in scripts
                )
                links = [attributes.get("href") for tag, attributes in page.elements if tag == "a"]
                assert prefix + "/planning-performance" in links
                if route == "/workspace":
                    frames = [attributes for tag, attributes in page.elements if tag == "iframe"]
                    assert len(frames) == 1
                    assert frames[0]["src"] == (
                        prefix + "/planning-performance?embedded=1#" + fragment
                    )
            response = await client.get("/v1/setup", headers=headers)
            assert response.status == (200 if prefix else 401)

    asyncio.run(scenario())
