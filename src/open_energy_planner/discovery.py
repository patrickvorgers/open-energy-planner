"""Pinned-TLS connection listener and mDNS advertisement. No HA APIs or planner logic."""

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from open_energy_planner.contracts.bonus_statistics import MAX_STATISTICS_REPORT_BYTES
from open_energy_planner.core.units import BYTES_PER_KIB
from open_energy_planner.runtime.diagnostics.logging import failure_reason

from .build_info import HEADER_FIELDS, RELEASE
from .home_assistant.routes import PEER_ROUTES
from .supervisor import connect as connect_supervisor

CERTIFICATE_VALIDITY_DAYS = 3650
PROXY_READ_CHUNK_BYTES = 16384
PROXY_TIMEOUT_SECONDS = 30
MAX_PROXY_RESPONSE_MIB = 4
DEFAULT_DISCOVERY_PORT = 8098
DEFAULT_APP_PORT = 8099
MDNS_PORT = 5353
DISCOVERY_PROBE_TIMEOUT_SECONDS = 10

SERVICE = "_oep._tcp.local."
LOG = logging.getLogger(__name__)
FORWARD = web.AppKey("forward", ClientSession)


def certificate(directory, instance_id):
    """Retain one self-signed TLS identity across restarts. Discovery publishes only its SHA256 pin."""
    root = Path(directory)
    key_path, cert_path = root / "connection-key.pem", root / "connection-cert.pem"
    if key_path.exists() != cert_path.exists():
        raise ValueError("Incomplete connection identity. Restore its matching certificate and key")
    if not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, instance_id)])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=CERTIFICATE_VALIDITY_DAYS))
            .sign(key, hashes.SHA256())
        )
        descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    pin = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return context, pin


def connection_app(pairing, token, upstream, ui_url):
    """Terminate TLS and isolate peer access from app-admin consent endpoints."""
    app = web.Application(client_max_size=BYTES_PER_KIB * BYTES_PER_KIB)

    async def context(app):
        """Own the single loopback forwarding session for the TLS listener lifetime."""
        async with ClientSession(timeout=ClientTimeout(total=PROXY_TIMEOUT_SECONDS)) as session:
            app[FORWARD] = session
            yield

    app.cleanup_ctx.append(context)

    async def handle(request):
        """Permit only TLS pairing requests or authenticated peer API calls. Never proxy admin consent."""
        if not request.secure:
            raise web.HTTPForbidden()
        if request.path == "/pair/info" and request.method == "GET":
            return web.json_response(
                {
                    "instance_id": pairing.instance_id,
                    "release": RELEASE,
                    "ui_url": pairing.ui_url or ui_url,
                }
            )
        if request.path == "/pair/request" and request.method == "POST":
            return web.json_response(pairing.request(await request.json()))
        if request.path == "/pair/claim" and request.method == "POST":
            return web.json_response(pairing.claim(await request.json()))
        if request.path == "/pair/supervisor" and request.method == "POST":
            return web.json_response(
                await pairing.supervisor_pair(await request.json()),
                headers={"Cache-Control": "no-store"},
            )
        if not pairing.authorized(request.headers.get("Authorization", "")):
            raise web.HTTPUnauthorized()
        if (request.method, request.path) not in PEER_ROUTES:
            raise web.HTTPForbidden()
        if request.path in ("/v1/history", "/v1/settlement/bonus-history"):
            request = request.clone(client_max_size=MAX_STATISTICS_REPORT_BYTES)
        async with app[FORWARD].request(
            request.method,
            upstream + request.rel_url.path_qs,
            data=await request.read(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                **{
                    name: request.headers[name]
                    for name in (*HEADER_FIELDS.values(), "X-OEP-View")
                    if name in request.headers
                },
            },
            allow_redirects=False,
        ) as response:
            content = bytearray()
            async for chunk in response.content.iter_chunked(PROXY_READ_CHUNK_BYTES):
                content.extend(chunk)
                if len(content) > MAX_PROXY_RESPONSE_MIB * BYTES_PER_KIB * BYTES_PER_KIB:
                    raise web.HTTPBadGateway()
            return web.Response(
                body=bytes(content),
                status=response.status,
                content_type="application/json",
                headers={
                    name: response.headers[name]
                    for name in (*HEADER_FIELDS.values(), "X-OEP-Mismatch")
                    if name in response.headers
                },
            )

    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def advertise(
    app,
    pairing,
    directory,
    token,
    *,
    port=DEFAULT_DISCOVERY_PORT,
    http_port=DEFAULT_APP_PORT,
    ui_url=None,
):
    """Install lifecycle-owned TLS and mDNS services for an explicit deployed app process."""

    async def lifetime(_app):
        """Start the listener before advertising and always close multicast sockets and HTTP tasks."""
        context, pin = await asyncio.to_thread(certificate, directory, pairing.instance_id)
        pairing.fingerprint = pin
        public_ui = ui_url or f"http://{socket.gethostname()}:{http_port}/setup"
        runner = web.AppRunner(
            connection_app(pairing, token, f"http://127.0.0.1:{http_port}", public_ui)
        )
        zc = None
        supervisor_task = None
        registered = False
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", port, ssl_context=context).start()
            try:
                address = await asyncio.to_thread(socket.gethostbyname, socket.gethostname())
                if ipaddress.ip_address(address).is_loopback:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                        probe.connect(("224.0.0.251", MDNS_PORT))
                        address = probe.getsockname()[0]
            except OSError:
                LOG.warning("Local address discovery unavailable. Application remains available")
                yield
                return
            supervisor = os.environ.get("SUPERVISOR_TOKEN")
            if supervisor:
                supervisor_task = asyncio.create_task(
                    connect_supervisor(pairing, supervisor, address, port, pin, ui_url=ui_url)
                )
                # Supervisor supplies this token. It is never an app/HA peer credential.
                async with ClientSession(
                    timeout=ClientTimeout(total=DISCOVERY_PROBE_TIMEOUT_SECONDS)
                ) as session:
                    try:
                        async with session.post(
                            "http://supervisor/discovery",
                            headers={"Authorization": "Bearer " + supervisor},
                            json={
                                "service": "open_energy_planner",
                                "config": {
                                    # Supervisor suppresses identical announcements after an app restart.
                                    "announcement_id": uuid4().hex,
                                    "host": address,
                                    "port": port,
                                    "instance_id": pairing.instance_id,
                                    "release": RELEASE,
                                    "fingerprint": pin,
                                },
                            },
                        ) as response:
                            response.raise_for_status()
                    except (ClientError, TimeoutError) as error:
                        LOG.warning(
                            "Supervisor discovery announcement failed (%s). The app remains available. Check Supervisor connectivity and the app discovery permission.",
                            failure_reason(error),
                        )
            info = ServiceInfo(
                SERVICE,
                f"OEP-{pairing.instance_id}.{SERVICE}",
                addresses=[ipaddress.ip_address(address).packed],
                port=port,
                properties={
                    "instance_id": pairing.instance_id,
                    "release": RELEASE,
                    "fingerprint": pin,
                },
                server=f"oep-{pairing.instance_id}.local.",
            )
            try:
                zc = AsyncZeroconf()
                await zc.async_register_service(info)
                registered = True
            except OSError:
                LOG.warning("Local discovery unavailable. Check multicast networking")
            yield
        finally:
            if supervisor_task:
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)
                pairing.supervisor_secret = None
            if zc:
                if registered:
                    await zc.async_unregister_service(info)
                await zc.async_close()
            await runner.cleanup()

    app.cleanup_ctx.append(lifetime)
