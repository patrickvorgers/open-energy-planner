"""Jeroen.nl public Netherlands forecast boundary over bounded JSON-RPC."""

from datetime import timedelta
from math import ceil, isfinite

from open_energy_planner.core.units import MINUTES_PER_HOUR

from ...planning_policy import PRICE_COVERAGE_HOURS
from ..model import PriceKind, PricePoint, PriceSeries, PricesUnavailable
from ._parsing import stamp

MAX_FORECAST_DAYS = 7
MAX_MODEL_AGE_HOURS = 27
NATIVE_INTERVAL_MINUTES = 15
HOURS_PER_CALENDAR_DAY = 24
CALENDAR_EDGE_DAYS = 2
MCP_PROTOCOL_VERSION = "2025-03-26"
# A fall-back calendar day can contain 25 hours of native quarters.
MAX_ROWS_PER_CALENDAR_DAY = (
    (HOURS_PER_CALENDAR_DAY + 1) * MINUTES_PER_HOUR // NATIVE_INTERVAL_MINUTES
)
MAX_FORECAST_ROWS = MAX_FORECAST_DAYS * MAX_ROWS_PER_CALENDAR_DAY


class JeroenPrices:
    """Keep published rows out of forecasts and retain the actual daily model issue time.

    Only bare Dutch market prices are requested. Contract fees, taxes and bonuses
    remain app-owned. P50 is the deterministic planning input. The provider's
    uncertainty band is validated but is not a stochastic solver constraint.
    """

    SOURCE_ID = "jeroen_nl"
    SUPPORTED_MARKETS = frozenset(("NL",))
    HORIZON_HOURS = PRICE_COVERAGE_HOURS
    ENDPOINT = "https://jeroen.nl/mcp"

    def __init__(self, client, market_area):
        """Borrow the bounded public client. Never send user or Home Assistant data."""
        self.client, self.market_area = client, market_area
        self.initialized = False

    async def fetch(self, now):
        """Fetch sufficient calendar coverage, clipping to the shared UTC price horizon."""
        if self.market_area not in self.SUPPORTED_MARKETS:
            raise PricesUnavailable("jeroen_market_area_unsupported")
        try:
            if not self.initialized:
                initialized = await self.client.post(
                    self.ENDPOINT,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "Open Energy Planner", "version": "0.2.0"},
                        },
                    },
                )
                if (
                    initialized.get("error")
                    or initialized["result"]["protocolVersion"] != MCP_PROTOCOL_VERSION
                ):
                    raise ValueError("Unsupported price protocol")
                self.initialized = True
            reply = await self.client.post(
                self.ENDPOINT,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_electricity_price_forecast",
                        "arguments": {
                            "days": min(
                                MAX_FORECAST_DAYS,
                                ceil(self.HORIZON_HOURS / HOURS_PER_CALENDAR_DAY)
                                + CALENDAR_EDGE_DAYS,
                            ),
                            "resolution": "quarter",
                            "include_consumer_price": False,
                        },
                    },
                },
            )
            result = reply.get("result")
            if reply.get("error") or not isinstance(result, dict) or result.get("isError"):
                raise ValueError("Price RPC failed")
            data = result["structuredContent"]
            if (
                data["source"] != "jeroen.nl"
                or data["unit"] != "EUR/kWh"
                or data["resolution"] != "quarter"
            ):
                raise ValueError("Unexpected price identity")
            columns = data["columns"]
            if not isinstance(columns, list) or len(columns) != len(set(columns)):
                raise ValueError("Invalid price columns")
            positions = {
                name: columns.index(name)
                for name in (
                    "time_utc",
                    "known_market_price",
                    "forecast_p10",
                    "forecast_p50",
                    "forecast_p90",
                )
            }
            issued = stamp(data["forecast_made_at_utc"])
            expiry = issued + timedelta(hours=MAX_MODEL_AGE_HOURS)
            if issued > now or expiry <= now:
                raise ValueError("Stale or future price model")
            rows = data["rows"]
            if not isinstance(rows, list) or len(rows) > MAX_FORECAST_ROWS:
                raise ValueError("Invalid price row count")
            start = now.replace(
                minute=now.minute // NATIVE_INTERVAL_MINUTES * NATIVE_INTERVAL_MINUTES,
                second=0,
                microsecond=0,
            )
            end = start + timedelta(hours=self.HORIZON_HOURS)
            points, previous = [], None
            for row in rows:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ValueError("Invalid price row")
                left = stamp(row[positions["time_utc"]])
                if (
                    left.minute % NATIVE_INTERVAL_MINUTES
                    or left.second
                    or left.microsecond
                    or previous is not None
                    and left <= previous
                ):
                    raise ValueError("Unordered or unaligned price row")
                previous = left
                if row[positions["known_market_price"]] is not None or not start <= left < end:
                    continue
                band = [
                    row[positions[name]]
                    for name in ("forecast_p10", "forecast_p50", "forecast_p90")
                ]
                if band[1] is None:
                    continue
                if (
                    any(type(value) not in (int, float) or not isfinite(value) for value in band)
                    or not band[0] <= band[1] <= band[2]
                ):
                    raise ValueError("Invalid forecast uncertainty band")
                points.append(
                    PricePoint(
                        left,
                        left + timedelta(minutes=NATIVE_INTERVAL_MINUTES),
                        band[1],
                        PriceKind.FORECAST,
                        NATIVE_INTERVAL_MINUTES,
                    )
                )
            return PriceSeries(self.SOURCE_ID, "NL", "EUR", now, issued, expiry, tuple(points))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
            raise PricesUnavailable("invalid_jeroen_forecast") from error
