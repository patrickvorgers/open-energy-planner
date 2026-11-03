"""App-owned consent and installation credentials. Discovery itself never grants access."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

MIN_SECRET_LENGTH = 24
CREDENTIAL_ENTROPY_BYTES = 48
PRIVATE_FILE_MODE = 0o600
MAX_PENDING_PAIRING_REQUESTS = 8
MIN_PEER_ID_LENGTH = 16
PAIRING_VERIFICATION_CODE_LENGTH = 16
PAIRING_REQUEST_ID_ENTROPY_BYTES = 24
MAX_PEER_NAME_LENGTH = 80
PAIRING_REQUEST_TTL_SECONDS = 300
MAX_PAIRING_FIELD_LENGTH = 128


def administration_token(directory):
    """Generate the internal administration secret once. Supervisor users never need to enter it."""
    path = Path(directory) / "administration-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        value = secrets.token_urlsafe(CREDENTIAL_ENTROPY_BYTES)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
    value = path.read_text()
    if len(value) < MIN_SECRET_LENGTH:
        raise ValueError("Administration identity is incomplete. Restore its saved secret")
    return value


class Pairing:
    """Own bounded five-minute requests and one persistent HA peer, separately from planning."""

    def __init__(self, directory, instance_id):
        """Load the confirmed peer. Pending requests deliberately do not survive app restarts."""
        self.path = Path(directory) / "pairing.json"
        self.instance_id = instance_id
        self.peer = json.loads(self.path.read_text()) if self.path.exists() else None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.fingerprint = None
        self.supervisor_secret = None
        self.ui_url = None

    def expire(self):
        """Remove expired requests, including their short-lived claim credentials."""
        now = time.monotonic()
        self.pending = {
            key_item: value_item
            for key_item, value_item in self.pending.items()
            if value_item["expires"] > now
        }

    def request(self, body):
        """Register an untrusted candidate with a private claim secret. Never approve it implicitly."""
        self.expire()
        if not self.fingerprint or self.peer or len(self.pending) >= MAX_PENDING_PAIRING_REQUESTS:
            raise web.HTTPConflict(text="Already paired or too many pending requests")
        if not isinstance(body, dict) or set(body) != {"peer_id", "name", "secret"}:
            raise web.HTTPBadRequest()
        if any(not isinstance(body[key_item], str) for key_item in body):
            raise web.HTTPBadRequest()
        if not body["secret"].isascii():
            raise web.HTTPBadRequest()
        if (
            not MIN_SECRET_LENGTH <= len(body["secret"]) <= MAX_PAIRING_FIELD_LENGTH
            or not 1 <= len(body["name"]) <= MAX_PEER_NAME_LENGTH
        ):
            raise web.HTTPBadRequest()
        if not MIN_PEER_ID_LENGTH <= len(body["peer_id"]) <= MAX_PEER_NAME_LENGTH:
            raise web.HTTPBadRequest()
        request_id = secrets.token_urlsafe(PAIRING_REQUEST_ID_ENTROPY_BYTES)
        self.pending = {
            key_item: value_item
            for key_item, value_item in self.pending.items()
            if value_item["peer_id"] != body["peer_id"]
        }
        self.pending[request_id] = {
            "peer_id": body["peer_id"],
            "name": body["name"],
            "proof": hashlib.sha256(body["secret"].encode("utf-8", "replace")).hexdigest(),
            "expires": time.monotonic() + PAIRING_REQUEST_TTL_SECONDS,
            "token": None,
        }
        return {"request_id": request_id, "instance_id": self.instance_id}

    def summary(self):
        """Expose candidate names only to the authenticated app UI, never claim secrets or tokens."""
        self.expire()
        return {
            "paired": self.peer is not None,
            "peer_name": self.peer["name"] if self.peer else None,
            "supervisor_managed": bool(self.peer and self.peer.get("supervisor_managed")),
            "pending": [
                {
                    "request_id": key,
                    "name": value["name"],
                    "peer_id": value["peer_id"],
                    "verification": hashlib.sha256(
                        (value["proof"] + self.fingerprint).encode()
                    ).hexdigest()[:PAIRING_VERIFICATION_CODE_LENGTH],
                }
                for key, value in self.pending.items()
                if value["token"] is None
            ],
        }

    async def approve(self, request_id):
        """Durably bind one explicitly approved candidate before making its credential claimable."""
        async with self.lock:
            self.expire()
            if self.peer or request_id not in self.pending:
                raise web.HTTPConflict(text="Request expired or a peer is already paired")
            candidate = self.pending[request_id]
            peer = {
                "peer_id": candidate["peer_id"],
                "name": candidate["name"],
                "token": secrets.token_urlsafe(CREDENTIAL_ENTROPY_BYTES),
            }
            writer = asyncio.create_task(asyncio.to_thread(self._save, peer))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await asyncio.gather(writer, return_exceptions=True)
                if not writer.cancelled() and writer.exception() is None:
                    self.peer = peer
                    candidate["token"] = peer["token"]
                    self.pending = {request_id: candidate}
                raise
            self.peer = peer
            candidate["token"] = peer["token"]
            self.pending = {request_id: candidate}

    def _save(self, peer):
        """Atomically replace private credential storage without publishing a partial identity."""
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w") as stream:
            json.dump(peer, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def claim(self, body):
        """Return an approved credential only over pinned TLS to the holder of its request secret."""
        self.expire()
        if not isinstance(body, dict) or set(body) != {"request_id", "secret"}:
            raise web.HTTPBadRequest()
        if any(
            not isinstance(value, str) or len(value) > MAX_PAIRING_FIELD_LENGTH
            for value in body.values()
        ):
            raise web.HTTPBadRequest()
        if not body["secret"].isascii():
            raise web.HTTPBadRequest()
        candidate = self.pending.get(body["request_id"])
        proof = hashlib.sha256(body["secret"].encode("utf-8", "replace")).hexdigest()
        if not candidate or not hmac.compare_digest(candidate["proof"], proof):
            raise web.HTTPNotFound()
        return (
            {"status": "approved", "token": candidate["token"]}
            if candidate["token"]
            else {"status": "pending"}
        )

    def authorized(self, header):
        """Authenticate the single confirmed peer. The app administration token is not a peer key."""
        return self.peer is not None and hmac.compare_digest(
            header.encode("utf-8", "replace"), ("Bearer " + self.peer["token"]).encode("utf-8")
        )

    async def supervisor_pair(self, body):
        """Bind the HA reached through the authenticated Supervisor bootstrap, without user consent steps."""
        if not isinstance(body, dict) or set(body) != {"peer_id", "name", "secret"}:
            raise web.HTTPBadRequest()
        if any(
            not isinstance(value, str) or not 1 <= len(value) <= MAX_PAIRING_FIELD_LENGTH
            for value in body.values()
        ):
            raise web.HTTPBadRequest()
        if not self.supervisor_secret or not hmac.compare_digest(
            body["secret"].encode("utf-8", "replace"), self.supervisor_secret.encode("utf-8")
        ):
            raise web.HTTPForbidden()
        async with self.lock:
            if self.peer and self.peer["peer_id"] != body["peer_id"]:
                raise web.HTTPConflict(
                    text="A different Home Assistant already owns this installation"
                )
            peer = {
                "peer_id": body["peer_id"],
                "name": body["name"],
                "token": self.peer["token"]
                if self.peer
                else secrets.token_urlsafe(CREDENTIAL_ENTROPY_BYTES),
                "supervisor_managed": True,
            }
            writer = asyncio.create_task(asyncio.to_thread(self._save, peer))
            try:
                await asyncio.shield(writer)
            finally:
                await asyncio.gather(writer, return_exceptions=True)
                if not writer.cancelled() and writer.exception() is None:
                    self.peer = peer
                    self.pending.clear()
            return {"token": peer["token"]}

    async def reset(self):
        """Explicitly revoke only the HA credential, retaining certificates, installation and history."""
        async with self.lock:
            writer = asyncio.create_task(asyncio.to_thread(self._save, None))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await asyncio.gather(writer, return_exceptions=True)
                if not writer.cancelled() and writer.exception() is None:
                    self.peer = None
                    self.pending.clear()
                raise
            self.peer = None
            self.pending.clear()


PAIRING_KEY = web.AppKey("pairing", Pairing)


def register_pairing(app, pairing):
    """Expose consent only behind the ordinary authenticated app interface."""
    app[PAIRING_KEY] = pairing

    async def status(request):
        """List pending candidates for the signed-in app user."""
        return web.json_response(pairing.summary())

    async def approve(request):
        """Confirm exactly one candidate selected by the user, never a discovery advertisement."""
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"request_id"}
            or not isinstance(body["request_id"], str)
        ):
            raise web.HTTPBadRequest()
        await pairing.approve(body["request_id"])
        return web.json_response({"status": "paired"})

    app.router.add_get("/v1/pairing", status)
    app.router.add_post("/v1/pairing/approve", approve)

    async def reset(request):
        """Handle an explicit app-user recovery action, never an anonymous claim or peer call."""
        if await request.json() != {}:
            raise web.HTTPBadRequest()
        await pairing.reset()
        return web.json_response({"status": "unpaired"})

    app.router.add_post("/v1/pairing/reset", reset)
