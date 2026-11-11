"""Pause real database responses without terminating the established TCP connection."""

import select
import socket
from contextlib import contextmanager, suppress
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import Event, Thread

from sqlalchemy.engine import make_url


@contextmanager
def paused_responses(url):
    """Forward a local test connection and retain server bytes while the returned gate is set."""
    target = make_url(url)
    postgres = target.drivername.startswith("postgresql")
    if postgres:
        _arguments, parameters = target.get_dialect()().create_connect_args(target)
        upstream_host = parameters.get("hostaddr") or parameters.get("host")
        upstream_port = parameters.get("port", 5432)
    else:
        upstream_host, upstream_port = target.host, target.port or 3306
    paused, received, stopped = Event(), Event(), Event()

    class Forwarder(BaseRequestHandler):
        """Retain a live peer that still accepts queries but cannot deliver its answers."""

        def handle(self):
            """Resume retained bytes on release so even an original unbounded driver can be drained."""
            with (
                suppress(OSError),
                socket.create_connection(
                    (upstream_host, upstream_port),
                    timeout=5,
                ) as upstream,
            ):
                backlog = bytearray()
                while not stopped.is_set():
                    if backlog and not paused.is_set():
                        self.request.sendall(backlog)
                        backlog.clear()
                    readable, _, _ = select.select((self.request, upstream), (), (), 0.01)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        if source is upstream:
                            if paused.is_set() or backlog:
                                received.set()
                                backlog.extend(data)
                            else:
                                self.request.sendall(data)
                        else:
                            upstream.sendall(data)

    with ThreadingTCPServer(("127.0.0.1", 0), Forwarder) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            proxied = target.set(port=server.server_address[1])
            if postgres:
                proxied = proxied.difference_update_query(("port",)).update_query_dict(
                    {"hostaddr": "127.0.0.1"}
                )
            else:
                proxied = proxied.set(host="127.0.0.1")
            yield proxied, paused, received
        finally:
            stopped.set()
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()
