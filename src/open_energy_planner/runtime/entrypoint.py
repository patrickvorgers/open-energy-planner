"""Start the app process with explicit demo/live mode and Supervisor ingress configuration."""

import argparse
import os

from aiohttp import web

from open_energy_planner.runtime.diagnostics.logging import configure_logging

from ..app import create_app
from .maintenance import create_managed_app

DEFAULT_APP_PORT = 8099
HTTP_SHUTDOWN_TIMEOUT_SECONDS = 60
# Supervisor forwards authenticated ingress requests from this bridge peer.
SUPERVISOR_INGRESS_PEER = "172.30.32.2"


def main():
    """Start the local HTTP app in the explicitly selected demo or live mode."""
    configure_logging()
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_APP_PORT)
    parser.add_argument("--data", default="/data")
    parser.add_argument(
        "--recover-storage",
        action="store_true",
        help="Restore the source of an unreleased database migration before starting",
    )
    parser.add_argument("--market-area", help="Enable price inputs for this bidding area, e.g. NL")
    parser.add_argument(
        "--ui-url",
        default=os.environ.get("OEP_PUBLIC_UI_URL"),
        help="Externally reachable standalone UI URL. Supervisor ingress is automatic",
    )
    args = parser.parse_args()
    if args.recover_storage:
        from open_energy_planner.storage.adapters.lifecycle.maintenance import recover_source

        recover_source(args.data)
    elif os.environ.get("OEP_RECOVER_STORAGE") == "1":
        from open_energy_planner.storage.adapters.lifecycle.maintenance import requested_recovery

        requested_recovery(args.data)
    from open_energy_planner.prices.bootstrap import configured_price_factory

    prices = configured_price_factory() if args.live else None
    if args.market_area:
        from open_energy_planner.prices.bootstrap import price_factory

        prices = price_factory(args.market_area.upper())
    from ..demand.weather import weather_factory
    from ..pairing import administration_token
    from ..solar.satellite import satellite_factory
    from ..solar.weather import solar_weather_factory

    web.run_app(
        create_managed_app(
            args.data,
            os.environ.get("OEP_API_TOKEN") or administration_token(args.data),
            factory=create_app,
            source="home_assistant" if args.live else "synthetic",
            price_factory=prices,
            weather_factory=weather_factory if args.live else None,
            solar_weather_factory=solar_weather_factory if args.live else None,
            satellite_factory=satellite_factory if args.live else None,
            discovery_options={"http_port": args.port, "ui_url": args.ui_url}
            if args.live
            else None,
            ingress_peer=SUPERVISOR_INGRESS_PEER if os.environ.get("SUPERVISOR_TOKEN") else None,
        ),
        host=args.host,
        port=args.port,
        shutdown_timeout=HTTP_SHUTDOWN_TIMEOUT_SECONDS,
        print=None,
    )
