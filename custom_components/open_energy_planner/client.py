"""Only local HTTP and JSON. Never import the app package."""

import json
import logging
from http import HTTPStatus
from time import monotonic
from urllib.parse import urlencode

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout, Fingerprint

from .const import BUILD_INFO
from .wire_limits import (
    MAX_API_RESPONSE_BYTES,
    MAX_APPLIANCE_RESPONSE_BYTES,
    MAX_SNAPSHOT_RESPONSE_BYTES,
)

RESPONSE_READ_CHUNK_BYTES = 16384

HTTP_TIMEOUT_SECONDS = 5
_LOGGER = logging.getLogger(__name__)


class _HTTPRejection(ValueError):
    """Preserve a response status when the client raises a domain error."""

    def __init__(self, message, *, status=None):
        """Keep the existing error text along with optional safe HTTP metadata."""
        super().__init__(message)
        self.status = status


class AuthenticationError(_HTTPRejection):
    """An installation token needs user attention."""


class ReleaseMismatchError(_HTTPRejection):
    """The loaded app and integration do not identify the same release build."""


class InstallationMismatchError(ValueError):
    """The connected app is not the installation recorded by this HA config entry."""


class SupersededInstallationExchangeError(ValueError):
    """A response arrived after the confirmed installation epoch changed."""


class LocalClient:
    """Exchange bounded JSON with the local app using the existing HA HTTP session."""

    def __init__(
        self,
        session: ClientSession,
        url: str,
        token: str,
        fingerprint=None,
        *,
        expected_instance_id=None,
    ):
        """Bind an installation URL/token without creating a separate HA connection or session."""
        self.session, self.url, self.token = session, url.rstrip("/"), token
        self.fingerprint = Fingerprint(bytes.fromhex(fingerprint)) if fingerprint else True
        self.expected_instance_id = expected_instance_id
        self.instance_confirmed = False
        self.instance_mismatch = False
        self.instance_generation = 0
        self._request_failures: dict[tuple[str, str], str] = {}

    async def request(self, method, path, body=None):
        """Send authenticated HTTP without redirects and require a bounded object response.

        Use a five-second deadline. Authentication rejection is distinguished from
        other HTTP, transport and malformed-response failures for user-facing recovery.
        Whole-plan, complete appliance and catalogue membership replies allow 4 MiB. Other endpoints
        allow 256 KiB.

        Raises:
            ReleaseMismatchError: If a marked 409 response rejects this build or a
                /v1/ response reports a different or missing version/build identity.
            InstallationMismatchError: If plan status identifies a different
                installation than the config entry. Further exchanges are blocked
                until fresh plan status identifies the original installation again.
            SupersededInstallationExchangeError: If a newer exchange already
                changed installation authority. Discard the obsolete response.
        """
        identity_status = (method, path) == ("GET", "/v1/ha/plan-status")
        if self.instance_mismatch and not identity_status:
            raise InstallationMismatchError("App installation identity changed")
        if (
            self.expected_instance_id is not None
            and not self.instance_confirmed
            and not identity_status
        ):
            await self.plan_status()
        operation = (method, path.partition("?")[0])
        started_at = monotonic()
        generation = self.instance_generation
        try:
            result = await self._exchange(method, path, body)
            if generation != self.instance_generation:
                raise SupersededInstallationExchangeError("Installation exchange was superseded")
            if self.expected_instance_id is not None:
                if (identity_status or "instance_id" in result) and result.get(
                    "instance_id"
                ) != self.expected_instance_id:
                    self.instance_generation += 1
                    self.instance_mismatch = True
                    self.instance_confirmed = False
                    raise InstallationMismatchError("App installation identity changed")
                if identity_status:
                    if self.instance_mismatch:
                        self.instance_generation += 1
                    self.instance_mismatch = False
                    self.instance_confirmed = True
        except SupersededInstallationExchangeError:
            raise
        except (ClientError, TimeoutError, ValueError, TypeError, KeyError) as error:
            if generation != self.instance_generation and not isinstance(
                error, InstallationMismatchError
            ):
                raise SupersededInstallationExchangeError(
                    "Installation exchange was superseded"
                ) from None
            failure = type(error).__name__
            if (
                isinstance(error, (ClientResponseError, _HTTPRejection))
                and error.status is not None
            ):
                failure = f"{failure} (HTTP {error.status})"
            level = (
                logging.DEBUG
                if self._request_failures.get(operation) == failure
                else logging.WARNING
            )
            self._request_failures[operation] = failure
            _LOGGER.log(
                level,
                "App request %s %s failed after %.3f s (%s).",
                *operation,
                monotonic() - started_at,
                failure,
            )
            raise
        if self._request_failures.pop(operation, None) is not None:
            _LOGGER.info(
                "App request %s %s recovered in %.3f s.", *operation, monotonic() - started_at
            )
        return result

    async def _exchange(self, method, path, body):
        """Perform one HTTP exchange and validate the bounded response and release."""
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
                raise AuthenticationError("Local API token rejected", status=response.status)
            if (
                response.status == HTTPStatus.CONFLICT
                and response.headers.get("X-OEP-Mismatch") == "true"
            ):
                raise ReleaseMismatchError(
                    "App rejected a different integration release build", status=response.status
                )
            response.raise_for_status()
            received = {
                "version": response.headers.get("X-OEP-Release"),
                "build": response.headers.get("X-OEP-Build") or None,
            }
            if path.startswith("/v1/") and any(
                received[field] != BUILD_INFO[field] for field in received
            ):
                raise ReleaseMismatchError(
                    "App and integration release builds differ", status=response.status
                )
            # Bound complete command and metadata projections independently of ordinary receipts.
            snapshot_response = path.partition("?")[0] in (
                "/v1/ha/plan",
                "/v1/ha/requests",
                "/v1/ha/catalogue",
            )
            limit = (
                MAX_APPLIANCE_RESPONSE_BYTES
                if path == "/v1/appliances" or path.startswith("/v1/appliances/")
                else MAX_SNAPSHOT_RESPONSE_BYTES
                if snapshot_response
                else MAX_API_RESPONSE_BYTES
            )
            data = bytearray()
            async for part in response.content.iter_chunked(RESPONSE_READ_CHUNK_BYTES):
                data.extend(part)
                if len(data) > limit:
                    raise ValueError("App response exceeds size limit")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Invalid app response")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
            return result

    async def plan_status(self):
        """Read the current app installation and authoritative command identity."""
        return await self.request("GET", "/v1/ha/plan-status")

    async def plan(self, plan_id):
        """Fetch commands only for the still-current plan identity."""
        return await self.request("GET", "/v1/ha/plan?" + urlencode({"plan_id": plan_id}))

    async def measurement_config(self, revision):
        """Fetch measurement bindings only for the current app configuration revision."""
        return await self.request(
            "GET", "/v1/ha/measurement-config?" + urlencode({"revision": revision})
        )

    async def service_requests(self):
        """Refresh HA configuration and data requests without transferring a plan."""
        return await self.request("GET", "/v1/ha/requests")

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
        """Confirm persisted intent without fetching a plan or historical financial data."""
        receipt = await self.request(
            "PUT", "/v1/enabled", {"enabled": value, "request_id": request_id}
        )
        if (
            receipt.keys() != {"accepted", "enabled", "request_id"}
            or receipt["accepted"] is not True
            or type(receipt["enabled"]) is not bool
            or receipt["enabled"] is not value
            or not isinstance(receipt["request_id"], str)
            or receipt["request_id"] != request_id
        ):
            raise ValueError("Invalid enable acknowledgement")
        return receipt

    async def acknowledge(self, plan_id):
        """Acknowledge local acceptance of the specified current app plan."""
        return await self.request("POST", "/v1/ack", {"plan_id": plan_id})
