"""EnergyPriceForecast EU free forecast-only API adapter."""

from datetime import timedelta

from ...planning_policy import PLANNING_HORIZON_HOURS
from ..model import PriceKind, PricePoint, PriceSeries, PricesUnavailable
from ._parsing import price_rows, stamp
from .json_client import JsonClient

FREE_API_HORIZON_HOURS = 48
KNOWN_ISSUE_MAX_AGE_HOURS = 6
UNKNOWN_ISSUE_MAX_AGE_HOURS = 1


class EnergyPriceForecast:
    """Normalize public forecast-only base prices at native resolution."""

    SOURCE_ID = "energy_price_forecast_eu"
    SUPPORTED_MARKETS = frozenset(
        (
            "DE-LU",
            "NL",
            "BE",
            "FR",
            "CZ",
            "PL",
            "FI",
            "SE1",
            "SE2",
            "SE3",
            "SE4",
            "DK1",
            "DK2",
            "AT",
            "NO1",
            "NO2",
            "NO3",
            "NO4",
            "NO5",
            "CH",
            "ITN",
            "IT_CNOR",
            "IT_CSUD",
            "IT_SUD",
            "IT_CALA",
            "IT_SICI",
            "IT_SARD",
            "SK",
            "RO",
            "ES",
            "PT",
            "GR",
            "BG",
            "GB",
        )
    )
    HORIZON_HOURS = min(PLANNING_HORIZON_HOURS, FREE_API_HORIZON_HOURS)
    ENDPOINT = "https://api.energypriceforecast.eu/api/v1/home-assistant/prices"

    def __init__(self, client: JsonClient, market_area: str):
        """Keep provider area aliases inside the adapter without adding user settings."""
        self.client, self.market_area = client, market_area
        self.provider_area = {"DE-LU": "de"}.get(market_area, market_area.lower())

    async def fetch(self, now):
        """Fetch the planning horizon within the free API limit. Retain native resolution and the actual model issue time.

        Six-hour maximum issue age is a provisional freshness bound, independent
        of hourly request frequency. A new HTTP receipt never refreshes an old model.
        """
        if self.market_area not in self.SUPPORTED_MARKETS:
            raise PricesUnavailable("price_forecast_market_area_unsupported")
        raw = await self.client.get(
            self.ENDPOINT,
            {
                "country": self.provider_area,
                "hours": str(self.HORIZON_HOURS),
                "mode": "forecast_only",
                "resolution": "native",
                "price_mode": "base",
                "currency": "EUR",
            },
        )
        try:
            if (
                not isinstance(raw.get("country"), str)
                or raw["country"].lower() != self.provider_area.lower()
                or raw["unit"] != "EUR/kWh"
                or raw.get("currency", "EUR") != "EUR"
            ):
                raise ValueError("Unexpected forecast identity")
            source = raw.get("source")
            model = source.get("firestore") if isinstance(source, dict) else None
            issue_text = model.get("updated_at") if isinstance(model, dict) else None
            issued = stamp(issue_text) if issue_text else None
            generated = stamp(raw["generated_at"]) if issued is None else issued
            if generated > now:
                raise ValueError("Forecast response is from the future")
            # Response creation is not a model issue time. Unknown model age gets
            # a shorter response-age bound and remains unknown in the archive.
            expiry = generated + timedelta(
                hours=KNOWN_ISSUE_MAX_AGE_HOURS
                if issued is not None
                else UNKNOWN_ISSUE_MAX_AGE_HOURS
            )
            if expiry <= now:
                raise PricesUnavailable("price_forecast_expired")
            points = []
            for row in price_rows(raw["entries"]):
                if row["source"] != "forecast" or row["unit"] != "EUR/kWh":
                    raise ValueError("Expected forecast-only base prices")
                start, end = stamp(row["start"]), stamp(row["end"])
                points.append(
                    PricePoint(
                        start,
                        end,
                        row["value"],
                        PriceKind.FORECAST,
                        row.get(
                            "native_resolution_minutes", int((end - start).total_seconds() / 60)
                        ),
                    )
                )
            return PriceSeries(
                self.SOURCE_ID,
                self.market_area,
                "EUR",
                now,
                issued,
                expiry,
                tuple(points),
            )
        except PricesUnavailable:
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise PricesUnavailable("invalid_price_forecast") from error
