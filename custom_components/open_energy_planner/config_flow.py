"""Manual connection fallback used by the first chain milestone."""

import asyncio
import hashlib
import ipaddress
import logging
import re
import secrets
from contextlib import suppress
from urllib.parse import urlsplit

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_instance_id
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import LocalClient
from .const import DOMAIN, RELEASE

PAIRING_CREDENTIAL_ENTROPY_BYTES = 32
PAIRING_VERIFICATION_CODE_LENGTH = 16
MAX_PEER_NAME_LENGTH = 80
MAX_CONSENT_POLLS = 150
DEFAULT_HTTPS_PORT = 443

DISCOVERY_RETRY_SECONDS = 2

MAX_TCP_PORT = 65535
IPV6_VERSION = 6


class PlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a local app connection after verifying its release and installation identity."""

    VERSION = 1

    async def async_step_zeroconf(self, discovery_info):
        """Start consent for a discovered TLS endpoint. An advertisement cannot authorize a peer."""
        from .supervisor import register

        register(self.hass)
        return await self._discovered(
            str(discovery_info.ip_address), discovery_info.port, discovery_info.properties
        )

    async def async_step_hassio(self, discovery_info):
        """Load authenticated bootstrap. Supervisor metadata alone is not authority."""
        from .supervisor import register

        register(self.hass)
        return self.async_abort(reason="supervisor_connecting")

    async def async_step_integration_discovery(self, discovery_info):
        """Finish the private administrator-authenticated handshake without user steps."""
        return await self._discovered(
            discovery_info.get("host"),
            discovery_info.get("port"),
            {**discovery_info, "release": RELEASE},
            automatic_secret=discovery_info.get("secret"),
        )

    async def _discovered(self, host, port, properties, *, automatic_secret=None):
        """Validate identity hints, keep existing certificate pins and deduplicate pending flows."""
        try:
            instance = properties["instance_id"]
            pin = properties["fingerprint"]
            if not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9-]{36}", instance):
                raise ValueError()
            if not isinstance(pin, str) or not re.fullmatch(r"[a-f0-9]{64}", pin):
                raise ValueError()
            if (
                properties.get("release") != RELEASE
                or type(port) is not int
                or not 1 <= port <= MAX_TCP_PORT
            ):
                raise ValueError()
            address = ipaddress.ip_address(host)
            if address.is_unspecified or address.is_multicast:
                raise ValueError()
            url = f"https://{'[' + str(address) + ']' if address.version == IPV6_VERSION else address}:{port}"
        except (KeyError, ValueError, TypeError):
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(instance)
        existing = self._async_current_entries()
        for entry in existing:
            if entry.unique_id == instance and entry.data.get("fingerprint"):
                if entry.data["fingerprint"] != pin:
                    return self.async_abort(reason="identity_changed")
                if not getattr(self, "_repair", False) and automatic_secret is None:
                    self._abort_if_unique_id_configured(updates={"url": url})
        self._pair_url, self._pair_pin, self._pair_instance = url, pin, instance
        self._pair_secret = secrets.token_urlsafe(PAIRING_CREDENTIAL_ENTROPY_BYTES)
        proof = hashlib.sha256(self._pair_secret.encode()).hexdigest()
        self._pair_code = hashlib.sha256((proof + pin).encode()).hexdigest()[
            :PAIRING_VERIFICATION_CODE_LENGTH
        ]
        self._pair_result = None
        self._automatic_secret = automatic_secret
        if automatic_secret is not None:
            await self._wait_for_consent()
            return await self.async_step_paired()
        self._pair_task = self.hass.async_create_background_task(
            self._wait_for_consent(), "OEP app consent"
        )
        return await self.async_step_pairing()

    async def _wait_for_consent(self):
        """Prove request ownership over pinned TLS. No integration entry exists until app approval."""
        client = LocalClient(async_get_clientsession(self.hass), self._pair_url, "", self._pair_pin)
        try:
            info = await client.request("GET", "/pair/info")
            if info.get("instance_id") != self._pair_instance or info.get("release") != RELEASE:
                raise ValueError("Discovered identity mismatch")
            secret = self._pair_secret
            if self._automatic_secret is not None:
                result = await client.request(
                    "POST",
                    "/pair/supervisor",
                    {
                        "peer_id": await async_instance_id(self.hass),
                        "name": self.hass.config.location_name[:MAX_PEER_NAME_LENGTH]
                        or "Home Assistant",
                        "secret": self._automatic_secret,
                    },
                )
                self._pair_result = {
                    "url": self._pair_url,
                    "token": result["token"],
                    "fingerprint": self._pair_pin,
                    "ui_url": info["ui_url"],
                    "supervisor": True,
                }
                return
            pending = await client.request(
                "POST",
                "/pair/request",
                {
                    "peer_id": await async_instance_id(self.hass),
                    "name": self.hass.config.location_name[:MAX_PEER_NAME_LENGTH]
                    or "Home Assistant",
                    "secret": secret,
                },
            )
            for _ in range(MAX_CONSENT_POLLS):
                result = await client.request(
                    "POST", "/pair/claim", {"request_id": pending["request_id"], "secret": secret}
                )
                if result.get("status") == "approved":
                    self._pair_result = {
                        "url": self._pair_url,
                        "token": result["token"],
                        "fingerprint": self._pair_pin,
                        "ui_url": info["ui_url"],
                    }
                    return
                await asyncio.sleep(DISCOVERY_RETRY_SECONDS)
        except (ClientError, TimeoutError, ValueError, KeyError):
            logging.getLogger(__name__).debug("App pairing failed", exc_info=True)

    async def async_step_pairing(self, user_input=None):
        """Wait without another confirmation, then bind or migrate the existing installation entry."""
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="app_consent",
                progress_task=self._pair_task,
                description_placeholders={"verification": self._pair_code},
            )
        self.hass.async_create_background_task(self._complete_pairing(), "OEP complete consent")
        return self.async_show_progress_done(next_step_id="paired")

    async def _complete_pairing(self):
        """Advance progress_done even when no HA browser has the discovery dialog open."""
        # Let HA's frontend consume progress_done first when its dialog is open.
        # The fallback finishes unattended discovery when there is no frontend.
        await asyncio.sleep(DISCOVERY_RETRY_SECONDS)
        with suppress(UnknownFlow):
            await self.hass.config_entries.flow.async_configure(self.flow_id)

    async def async_step_paired(self, user_input=None):
        """Finish only after the progress step has made its required completion transition."""
        if self._pair_result is None:
            return self.async_abort(reason="pairing_failed")
        if self.context.get("source") == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=self._pair_result
            )
        # Legacy URL/token connections migrate in place, preserving entity/history identities.
        self._abort_if_unique_id_configured(updates=self._pair_result)
        return self.async_create_entry(title="Open Energy Planner", data=self._pair_result)

    async def async_step_reauth(self, entry_data):
        """Recover a revoked credential through fresh app consent while retaining the pinned server identity."""
        if entry_data.get("supervisor"):
            return self.async_abort(reason="supervisor_connecting")
        if not entry_data.get("fingerprint"):
            return await self.async_step_user()
        self._repair = True
        entry = self._get_reauth_entry()
        address = urlsplit(entry_data["url"])
        return await self._discovered(
            address.hostname,
            address.port or DEFAULT_HTTPS_PORT,
            {
                "instance_id": entry.unique_id,
                "release": RELEASE,
                "fingerprint": entry_data["fingerprint"],
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Link an existing connection to its app-owned setup interface."""
        return PlannerOptionsFlow()

    async def async_step_user(self, user_input=None):
        """Validate the local URL/token and prevent duplicate installation entries."""
        errors = {}
        if user_input:
            try:
                url = urlsplit(user_input["url"])
                if url.scheme not in ("http", "https") or not url.hostname or url.username:
                    raise ValueError("Invalid local URL")
                data = await LocalClient(
                    async_get_clientsession(self.hass), user_input["url"], user_input["token"]
                ).snapshot()
                if data.get("release") != RELEASE:
                    raise ValueError("Release mismatch")
                await self.async_set_unique_id(data["instance_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Open Energy Planner", data=user_input)
            except (ClientError, TimeoutError, ValueError, KeyError):
                logging.getLogger(__name__).debug(
                    "Local EMS connection check failed", exc_info=True
                )
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("url", default="http://open-energy-planner:8099"): str,
                    vol.Required("token"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )


class PlannerOptionsFlow(config_entries.OptionsFlow):
    """Link to app-owned setup. The integration has no installation configuration wizard."""

    async def async_step_init(self, user_input=None):
        """Expose the app link without collecting sensor, physical or forecast settings in HA."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "url": self.config_entry.data.get(
                    "ui_url", self.config_entry.data["url"].rstrip("/") + "/setup"
                )
            },
            data_schema=vol.Schema({}),
        )
