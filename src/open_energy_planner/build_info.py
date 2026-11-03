"""Read immutable packaged identity once, independent of runtime Git or environment variables."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from aiohttp import web

BUILD_INFO = json.loads(Path(__file__).with_name("build-info.json").read_text(encoding="utf-8"))
RELEASE = BUILD_INFO["version"]
INTEGRATION_CONTACT_MAX_AGE_SECONDS = 30
HEADER_FIELDS = {"version": "X-OEP-Release", "build": "X-OEP-Build"}
BUILD_STATUS = web.AppKey("build_status", object)
_LOGGER = logging.getLogger(__name__)


def register(app):
    """Check every identified integration request and expose current process identities for deployment checks.

    Authentication runs before this middleware. Missing build/revision values mean
    development sources, never a verified build. An observed integration expires
    after 30 seconds without contact. No identity is persisted across app restarts.
    """
    integration = None
    last_seen = None
    last_contact = None
    last_mismatch = None

    @web.middleware
    async def identity(request, handler):
        """Reject a different peer build before its handler runs and identify successful API responses."""
        nonlocal integration, last_seen, last_contact, last_mismatch
        if request.path.startswith("/v1/") and (
            "X-OEP-Release" in request.headers or request.headers.get("X-OEP-View") == "commands"
        ):
            integration = {
                field: request.headers.get(header) or None
                for field, header in HEADER_FIELDS.items()
            }
            last_seen = datetime.now(UTC)
            last_contact = monotonic()
            if any(integration[field] != BUILD_INFO[field] for field in HEADER_FIELDS):
                identity = tuple(integration[field] for field in HEADER_FIELDS)
                if identity != last_mismatch:
                    _LOGGER.info(
                        "Integration data blocked: release_mismatch. Install the app and integration "
                        "from the same installation bundle."
                    )
                    last_mismatch = identity
                return web.json_response(
                    {"error": "release_mismatch", "app": BUILD_INFO},
                    status=409,
                    headers={
                        **{
                            header: BUILD_INFO[field] or ""
                            for field, header in HEADER_FIELDS.items()
                        },
                        "X-OEP-Mismatch": "true",
                    },
                )
            if last_mismatch is not None:
                _LOGGER.info("Integration data accepted again. App and integration builds match")
                last_mismatch = None
        response = await handler(request)
        if request.path.startswith("/v1/"):
            response.headers.update(
                {header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()}
            )
        return response

    def status():
        """Report loaded identities and recent peer contact without implying that a stale peer still runs.

        ``blocking_mismatch`` retains an observed mismatch beyond the contact
        window, including when status is unavailable. It clears only after an
        identified /v1/ request reports the matching app build.
        """
        recent = (
            last_contact is not None
            and monotonic() - last_contact <= INTEGRATION_CONTACT_MAX_AGE_SECONDS
        )
        return {
            "app": BUILD_INFO,
            "integration": integration,
            "integration_last_seen": last_seen.isoformat() if last_seen else None,
            "blocking_mismatch": last_mismatch is not None,
            "status": "unavailable"
            if not recent
            else "matched"
            if all(integration[field] == BUILD_INFO[field] for field in HEADER_FIELDS)
            else "mismatch",
        }

    async def version(request):
        """Return authenticated process provenance for post-deployment verification."""
        return web.json_response(status(), headers={"Cache-Control": "no-store"})

    app[BUILD_STATUS] = status
    app.middlewares.append(identity)
    app.router.add_get("/v1/version", version)
