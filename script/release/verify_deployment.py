"""Verify both running components against the immutable manifest from their installation bundle."""

import argparse
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEPLOYMENT_READ_TIMEOUT_SECONDS = 10
MAX_VERSION_RESPONSE_BYTES = 16 * 1024


def validate_url(url):
    """Require HTTPS except for loopback so the deployment token cannot cross a LAN in cleartext."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Use an HTTP(S) app base URL without embedded credentials")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Non-loopback deployment checks require HTTPS")


class NoRedirect(HTTPRedirectHandler):
    """Keep the installation token at the explicitly selected deployment URL."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Reject redirects instead of forwarding the authorization header to another destination."""
        return


def verify(status, manifest):
    """Reject missing, stale or different loaded identities, even when both installed components agree."""
    expected = {field: manifest[field] for field in ("version", "build")}
    if manifest.get("source_state") != "commit":
        raise ValueError("Deployment requires a committed-source installation bundle")
    if not isinstance(expected["build"], str) or not re.fullmatch(
        r"[1-9][0-9]*\.[1-9][0-9]*", expected["build"]
    ):
        raise ValueError("Deployment requires a numbered CI build")
    for component in ("app", "integration"):
        loaded = status.get(component)
        if (
            status.get("status") != "matched"
            or not isinstance(loaded, dict)
            or any(loaded.get(field) != value for field, value in expected.items())
        ):
            raise ValueError(
                "Running app/integration do not match the expected installation bundle"
            )


def main():
    """Read process identities using the existing API token, without printing credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="App base URL, including its ingress prefix when applicable")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    validate_url(args.url)
    request = Request(
        args.url.rstrip("/") + "/v1/version",
        headers={"Authorization": "Bearer " + os.environ["OEP_API_TOKEN"]},
    )
    with build_opener(NoRedirect).open(
        request, timeout=DEPLOYMENT_READ_TIMEOUT_SECONDS
    ) as response:
        payload = response.read(MAX_VERSION_RESPONSE_BYTES + 1)
        if len(payload) > MAX_VERSION_RESPONSE_BYTES:
            raise ValueError("Version response exceeds size limit")
        status = json.loads(payload)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    verify(status, manifest)
    print(f"Verified app and integration: {manifest['version']}: Build: {manifest['build']}")


if __name__ == "__main__":
    main()
