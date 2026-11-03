"""Supervisor-authenticated bootstrap only. No HA state/history client or planner logic."""

import asyncio
import logging
import secrets

from aiohttp import ClientError, ClientSession, ClientTimeout

CREDENTIAL_ENTROPY_BYTES = 48
SUPERVISOR_TIMEOUT_SECONDS = 15
WEBSOCKET_HEARTBEAT_SECONDS = 20
MAX_SUPERVISOR_RESPONSE_BYTES = 262144
PAIRING_RETRY_SECONDS = 10
INTEGRATION_DISCOVERY_RETRY_SECONDS = 30

LOG = logging.getLogger(__name__)


async def connect(pairing, token, address, port, pin, *, ui_url=None):
    """Retry the custom integration bootstrap through Supervisor, retaining no user HA token.

    The random proof travels only through Supervisor's authenticated WebSocket
    proxy and the pinned TLS reply. Public discovery never carries this proof.
    Repeated handshakes recover lost HA credentials without changing ownership.
    """
    pairing.supervisor_secret = secrets.token_urlsafe(CREDENTIAL_ENTROPY_BYTES)
    async with ClientSession(timeout=ClientTimeout(total=SUPERVISOR_TIMEOUT_SECONDS)) as session:
        while True:
            try:
                if not ui_url:
                    async with session.get(
                        "http://supervisor/addons/self/info",
                        headers={"Authorization": "Bearer " + token},
                        allow_redirects=False,
                    ) as response:
                        response.raise_for_status()
                        slug = (await response.json())["data"]["slug"]
                        if (
                            not isinstance(slug, str)
                            or not slug.replace("_", "").replace("-", "").isalnum()
                        ):
                            raise ValueError("Invalid Supervisor app identity")
                        pairing.ui_url = "/app/" + slug
                async with session.ws_connect(
                    "http://supervisor/core/websocket",
                    heartbeat=WEBSOCKET_HEARTBEAT_SECONDS,
                    max_msg_size=MAX_SUPERVISOR_RESPONSE_BYTES,
                ) as websocket:
                    if (await websocket.receive_json())["type"] != "auth_required":
                        raise ValueError("Unexpected Supervisor authentication handshake")
                    await websocket.send_json({"type": "auth", "access_token": token})
                    if (await websocket.receive_json())["type"] != "auth_ok":
                        raise ValueError("Supervisor authentication rejected")
                    number = 0
                    while True:
                        number += 1
                        await websocket.send_json(
                            {
                                "id": number,
                                "type": "open_energy_planner/supervisor_link",
                                "host": address,
                                "port": port,
                                "instance_id": pairing.instance_id,
                                "fingerprint": pin,
                                "secret": pairing.supervisor_secret,
                            }
                        )
                        result = await asyncio.wait_for(
                            websocket.receive_json(), SUPERVISOR_TIMEOUT_SECONDS
                        )
                        if not isinstance(result, dict):
                            raise TypeError("Invalid Supervisor connection result")
                        if not result.get("success"):
                            error = result.get("error")
                            error_code = error.get("code") if isinstance(error, dict) else None
                            if error_code == "unknown_command":
                                LOG.warning(
                                    "The OEP integration is not loaded in Home Assistant. "
                                    "Its installation cannot be verified. Install the matching "
                                    "integration if needed, then restart Home Assistant Core. "
                                    "Automatic pairing will retry"
                                )
                            elif error_code == "link_failed":
                                LOG.warning(
                                    "The OEP integration is loaded in Home Assistant, but "
                                    "automatic pairing was not completed. Check the OEP "
                                    "integration logs in Home Assistant. Automatic pairing will retry"
                                )
                            else:
                                LOG.warning(
                                    "Home Assistant rejected the automatic OEP connection request. "
                                    "The integration's loaded state could not be verified. "
                                    "Check the Home Assistant logs. Automatic pairing will retry"
                                )
                            break
                        if number == 1:
                            LOG.info(
                                "The OEP integration is loaded in Home Assistant. "
                                "The automatic connection request was accepted"
                            )
                        await asyncio.sleep(INTEGRATION_DISCOVERY_RETRY_SECONDS)
                await asyncio.sleep(PAIRING_RETRY_SECONDS)
            except (ClientError, TimeoutError, ValueError, KeyError, TypeError):
                LOG.warning(
                    "Automatic Supervisor connection unavailable. Retrying without requesting user credentials"
                )
                await asyncio.sleep(PAIRING_RETRY_SECONDS)
