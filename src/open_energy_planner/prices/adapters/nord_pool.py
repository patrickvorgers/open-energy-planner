"""Nord Pool published-price translation. Provider aliases and units stay here."""

from ..model import PriceKind, PricePoint, PriceSeries, PricesUnavailable
from ._parsing import price_rows, stamp
from .json_client import JsonClient


class NordPoolPrices:
    """Translate the public Nord Pool portal's final EUR/MWh day-ahead prices.

    This is the public portal endpoint, not a subscription to the commercial
    Market Data API. Distribution/access rights remain a release admission item.
    """

    SOURCE_ID = "nord_pool"
    ENDPOINT = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
    KWH_PER_MWH = 1000

    def __init__(self, client: JsonClient, market_area: str):
        """Configure one bidding area. No provider selectors enter planner contracts."""
        self.client, self.market_area = client, market_area
        self.provider_area = {"DE-LU": "GER"}.get(market_area, market_area)

    async def fetch_day(self, day, now):
        """Fetch final area prices. Convert EUR/MWh to EUR/kWh exactly once."""
        raw = await self.client.get(
            self.ENDPOINT,
            {
                "date": day.isoformat(),
                "market": "DayAhead",
                "deliveryArea": self.provider_area,
                "currency": "EUR",
            },
        )
        try:
            if (
                raw["currency"] != "EUR"
                or raw["market"] != "DayAhead"
                or raw["deliveryDateCET"] != day.isoformat()
            ):
                raise ValueError("Unexpected price identity")
            if not any(
                source["state"] == "Final" and self.provider_area in source["areas"]
                for source in raw["areaStates"]
            ):
                raise PricesUnavailable("published_prices_not_final")
            points = []
            for row in price_rows(raw["multiAreaEntries"]):
                start, end = stamp(row["deliveryStart"]), stamp(row["deliveryEnd"])
                value = row["entryPerArea"][self.provider_area]
                if type(value) not in (int, float):
                    raise ValueError("Missing or invalid market price")
                points.append(
                    PricePoint(
                        start,
                        end,
                        value / self.KWH_PER_MWH,
                        PriceKind.PUBLISHED,
                        int((end - start).total_seconds() / 60),
                    )
                )
            return PriceSeries(
                self.SOURCE_ID,
                self.market_area,
                "EUR",
                now,
                stamp(raw["updatedAt"]) if raw.get("updatedAt") else None,
                None,
                tuple(points),
            )
        except PricesUnavailable:
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise PricesUnavailable("invalid_published_prices") from error
