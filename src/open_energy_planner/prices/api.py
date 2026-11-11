"""Register cached price and contract projections without fetching providers in HTTP requests."""

import logging
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.model import TariffCalculator
from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS
from open_energy_planner.runtime.diagnostics.logging import request_error_message

from .adapters.home_assistant import bridge

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    "Choose Automatic or an available forecast source for every lead period",
    "Configure the market area before selecting price sources",
    "Expected source preferences and their configuration version",
    "Forecast source does not support the selected lead period",
    "Market area changed. Reload before saving price preferences",
    "Price source preferences changed. Reload before saving",
)


def register(app, current_prices, contracts, live, lock):
    """Register cached price projections and the peer market-price input route.

    HTTP requests never fetch upstream providers.
    """

    def cached_document():
        """Project one current cache and route for both reads and confirmed preference saves."""
        prices = current_prices()
        if prices is None:
            return {"status": "not_configured"}
        now = datetime.now(UTC)
        document = prices.document(now)
        source = bridge(prices)
        if source is not None:
            document["published_source"] = source.document(now)
        return document

    async def price_snapshot(_request):
        """Return normalized price coverage/provenance without fetching during an API request."""
        return web.json_response(cached_document())

    async def save_price_preferences(request):
        """Persist manual source preferences without changing published prices or old plans."""
        if current_prices() is None or not hasattr(current_prices(), "save_preferences"):
            raise web.HTTPBadRequest(text="Configure market prices before selecting sources")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {
                "preferences",
                "configuration_version",
                "market_area",
            }:
                raise ValueError("Expected source preferences and their configuration version")
            await current_prices().save_preferences(
                body["preferences"], body["configuration_version"], body["market_area"]
            )
            async with lock:
                if live:
                    await live.refresh_async(force=True)
            return web.json_response(cached_document())
        except (ValueError, TypeError, KeyError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=logging.getLogger(__name__),
                    operation="Price source preferences",
                    safe_messages=PUBLIC_VALIDATION_MESSAGES,
                    fallback="Price source preferences could not be confirmed. Refresh source settings before retrying.",
                )
            ) from None
        except OSError:
            raise web.HTTPServiceUnavailable(
                text="Price source preferences could not be saved"
            ) from None

    async def current_price_inputs(_request):
        """Read current saved rates independently of plans, forecasts of consumption and bonus counters.

        Only accepted cached market prices are read. No provider fetch or write
        occurs. Missing bonus eligibility remains unknown while base rates stay
        visible. Archived plan evidence is never repriced by this endpoint.
        """
        async with lock:
            now, contract = datetime.now(UTC), contracts.current
            if contract is None or contracts.issue:
                return web.json_response(
                    {"issue": contracts.issue or "contract_not_configured", "intervals": []}
                )
            if current_prices() is None:
                return web.json_response({"issue": "market_prices_unavailable", "intervals": []})
            try:
                timeline = current_prices().timeline(
                    now,
                    hours=PLANNING_HORIZON_HOURS,
                    market_area=contract.market_area,
                    currency=contract.currency,
                )
                tariffs = TariffCalculator().calculate(timeline, contract)
                location = live.report.location if live and live.report else None
                preview = BonusCompiler().preview(contract, tariffs, location=location)
            except (ValueError, TypeError, KeyError, OverflowError):
                return web.json_response(
                    {"issue": "contract_or_market_prices_invalid", "intervals": []}
                )
            return web.json_response(
                {
                    "as_of": now.isoformat(),
                    "contract_version": contract.version,
                    "currency": contract.currency,
                    "coverage": timeline.status,
                    "issue": "market_prices_unavailable" if not preview else None,
                    "has_bonus": any(
                        contract.bonus_at(
                            item.tariff.start + (item.tariff.end - item.tariff.start) / 2
                        )
                        is not None
                        for item in preview
                    ),
                    "intervals": [
                        {
                            "start": item.tariff.start.isoformat(),
                            "end": item.tariff.end.isoformat(),
                            "market_price": item.tariff.market_price,
                            "buy_price": item.tariff.buy_price,
                            "sell_price": item.tariff.sell_price,
                            "total_export_price": item.tariff.sell_price + item.bonus_rate
                            if item.bonus_rate is not None
                            else None,
                            "bonus_issue": item.issue,
                            "price_kind": item.tariff.price_kind,
                            "source_id": item.tariff.source_id,
                        }
                        for item in preview
                    ],
                }
            )

    from open_energy_planner.home_assistant.market_prices import register as register_market_prices

    register_market_prices(app, current_prices)
    app.router.add_get("/v1/prices", price_snapshot)
    app.router.add_put("/v1/prices/preferences", save_price_preferences)
    app.router.add_get("/v1/prices/inputs", current_price_inputs)
