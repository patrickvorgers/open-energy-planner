"""Exercise real HTTP rendering against forged ingress paths and hostile page fragments."""

import asyncio
from html.parser import HTMLParser

import pytest
from aiohttp.test_utils import TestClient, TestServer

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
                    assert frames[0]["data-start-page"] == (
                        prefix + "/planning-performance?embedded=1#" + fragment
                    )
            response = await client.get("/v1/setup", headers=headers)
            assert response.status == (200 if prefix else 401)

    asyncio.run(scenario())
