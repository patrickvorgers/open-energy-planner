"""Only local HTTP and JSON. Never import the app package."""

import json
from http import HTTPStatus

from aiohttp import ClientSession, ClientTimeout, Fingerprint

from .const import BUILD_INFO
from .wire_limits import MAX_API_RESPONSE_BYTES, MAX_SNAPSHOT_RESPONSE_BYTES

RESPONSE_READ_CHUNK_BYTES = 16384

HTTP_TIMEOUT_SECONDS = 5


class AuthenticationError(ValueError):
    """An installation token needs user attention."""


class ReleaseMismatchError(ValueError):
    """The loaded app and integration do not identify the same release build."""


class LocalClient:
    """Exchange bounded JSON with the local app using the existing HA HTTP session."""

    def __init__(self, session: ClientSession, url: str, token: str, fingerprint=None):
        """Bind an installation URL/token without creating a separate HA connection or session."""
        self.session, self.url, self.token = session, url.rstrip("/"), token
        self.fingerprint = Fingerprint(bytes.fromhex(fingerprint)) if fingerprint else True

    async def request(self, method, path, body=None):
        """Send authenticated HTTP without redirects and require a bounded object response.

        Use a five-second deadline. Authentication rejection is distinguished from
        other HTTP, transport and malformed-response failures for user-facing recovery.
        Whole-plan and catalogue membership replies allow 4 MiB. Other endpoints
        allow 256 KiB.

        Raises:
            ReleaseMismatchError: If a marked 409 response rejects this build or a
                /v1/ response reports a different or missing version/build identity.
        """
        async with self.session.request(
            method,
            self.url + path,
            json=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-OEP-View": "commands",
                "X-OEP-Release": BUILD_INFO["version"],
                "X-OEP-Build": BUILD_INFO["build"] or "",
            },
            timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
            allow_redirects=False,
            ssl=self.fingerprint,
        ) as response:
            if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                raise AuthenticationError("Local API token rejected")
            if (
                response.status == HTTPStatus.CONFLICT
                and response.headers.get("X-OEP-Mismatch") == "true"
            ):
                raise ReleaseMismatchError("App rejected a different integration release build")
            response.raise_for_status()
            received = {
                "version": response.headers.get("X-OEP-Release"),
                "build": response.headers.get("X-OEP-Build") or None,
            }
            if path.startswith("/v1/") and any(
                received[field] != BUILD_INFO[field] for field in received
            ):
                raise ReleaseMismatchError("App and integration release builds differ")
            # Settings may return a complete snapshot too. Bound by the response contract.
            snapshot_response = path in (
                "/v1/snapshot",
                "/v1/observations",
                "/v1/enabled",
                "/v1/history",
                "/v1/demand/history",
                "/v1/solar/history",
                "/v1/sources",
                "/v1/replan",
                "/v1/ha/catalogue",
            ) or (method == "PUT" and path == "/v1/installation")
            limit = MAX_SNAPSHOT_RESPONSE_BYTES if snapshot_response else MAX_API_RESPONSE_BYTES
            data = bytearray()
            async for part in response.content.iter_chunked(RESPONSE_READ_CHUNK_BYTES):
                data.extend(part)
                if len(data) > limit:
                    raise ValueError("App response exceeds size limit")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Invalid app response")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
            return result

    async def snapshot(self):
        """Fetch installation status and the complete current plan from the app."""
        return await self.request("GET", "/v1/snapshot")

    async def configure_sources(self, bindings, revision):
        """Replace selected sources with compare-and-set revision protection."""
        return await self.request(
            "PUT", "/v1/sources", {"bindings": bindings, "expected_revision": revision}
        )

    async def observations(self, body):
        """Forward normalized source readings/issues over the existing authenticated connection."""
        return await self.request("POST", "/v1/observations", body)

    async def history(self, body):
        """Forward normalized historical energy evidence. Calculations remain in the app."""
        return await self.request("POST", "/v1/history", body)

    async def demand_history(self, body):
        """Forward normalized household history separately from bonus-counter evidence."""
        return await self.request("POST", "/v1/demand/history", body)

    async def configure_demand(self, body):
        """Persist the selected household energy boundary and annual estimate in the app."""
        return await self.request("PUT", "/v1/demand", body)

    async def enabled(self, value, request_id):
        """Send enable intent with a request identity so repeated synchronization is idempotent."""
        return await self.request(
            "PUT", "/v1/enabled", {"enabled": value, "request_id": request_id}
        )

    async def acknowledge(self, plan_id):
        """Acknowledge local acceptance of the specified current app plan."""
        return await self.request("POST", "/v1/ack", {"plan_id": plan_id})
