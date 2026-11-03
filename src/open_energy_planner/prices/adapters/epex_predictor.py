"""EpexPredictor public hourly forecast adapter with known-price exclusion."""

from datetime import timedelta

from open_energy_planner.core.units import MINUTES_PER_HOUR

from ...planning_policy import PRICE_COVERAGE_HOURS
from ..model import PriceKind, PricePoint, PriceSeries, PricesUnavailable
from ._parsing import price_rows, stamp
from .json_client import JsonClient


class EpexPredictor:
    """Normalize public hourly base forecasts. Never treat known prices as predictions."""

    SOURCE_ID = "epex_predictor"
    HORIZON_HOURS = PRICE_COVERAGE_HOURS
    ENDPOINT = "https://epexpredictor.batzill.com/prices"
    RESPONSE_MAX_AGE_HOURS = 2

    SUPPORTED_MARKETS = frozenset(
        ("DE-LU", "AT", "BE", "FR", "NL", "SE1", "SE2", "SE3", "SE4", "DK1", "DK2", "ES", "PT")
    )

    def __init__(self, client: JsonClient, market_area: str):
        """Keep supported regions, URL and fixed wholesale units inside this adapter."""
        self.client, self.market_area = client, market_area

    async def fetch(self, now):
        """Request extended policy coverage in UTC and EUR/kWh, excluding hours containing known quarters.

        The API has no model issue timestamp. Retain that uncertainty and allow
        the received response for two hours only. Receipt is not model freshness.
        Hourly refresh and a 30-minute failure retry fit before this bound.
        The inclusive endpoint can return a 73rd hour, which is discarded.
        """
        if self.market_area not in self.SUPPORTED_MARKETS:
            raise PricesUnavailable("epex_market_area_unsupported")
        start = now.replace(minute=0, second=0, microsecond=0)
        raw = await self.client.get(
            self.ENDPOINT,
            {
                "region": "DE" if self.market_area == "DE-LU" else self.market_area,
                "hours": str(self.HORIZON_HOURS),
                "unit": "EUR_PER_KWH",
                "hourly": "true",
                "timezone": "UTC",
                "surcharge": "0",
                "taxPercent": "0",
            },
        )
        try:
            known = stamp(raw["knownUntil"])
            points = []
            previous = None
            for row in price_rows(raw["prices"]):
                left = stamp(row["startsAt"])
                if (
                    left.minute
                    or left.second
                    or left.microsecond
                    or (previous is not None and left <= previous)
                ):
                    raise ValueError("Unordered or unaligned hourly prices")
                previous = left
                point = PricePoint(
                    left,
                    left + timedelta(hours=1),
                    row["total"],
                    PriceKind.FORECAST,
                    MINUTES_PER_HOUR,
                )
                if known < left and start <= left < start + timedelta(hours=self.HORIZON_HOURS):
                    points.append(point)
            return PriceSeries(
                self.SOURCE_ID,
                self.market_area,
                "EUR",
                now,
                None,
                now + timedelta(hours=self.RESPONSE_MAX_AGE_HOURS),
                tuple(points),
            )
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise PricesUnavailable("invalid_epex_forecast") from error
