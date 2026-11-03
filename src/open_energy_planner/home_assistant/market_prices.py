"""Queue authenticated HA market prices for admission by the existing refresh owner."""

from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.prices.adapters.home_assistant import bridge


def price_request(prices, now):
    """Expose provider work only for an enabled automatic HA price adapter."""
    source = bridge(prices)
    return source.request(now) if source else None


def register(app, current_prices):
    """Register one peer input route without granting configuration or retail-price writes."""

    async def receive(request):
        """Queue atomically on the event loop without waiting for provider or storage locks.

        Resolve the current source after reading the body. No further await may
        separate source lookup, validation and queueing. The refresh owner alone
        admits durable prices. Waiting behind its network work would keep peer
        HTTP requests alive during app shutdown.
        """
        try:
            body = await request.json()
            prices = current_prices()
            source = bridge(prices)
            if source is None:
                raise web.HTTPConflict(text="Home Assistant market prices are not configured")
            now = datetime.now(UTC)
            changed, candidates = source.receive(body, now)
            service = getattr(prices, "service", prices)
            if changed or candidates:
                service.attempts.clear()
            return web.json_response({"accepted": True})
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise web.HTTPBadRequest(text="Invalid Home Assistant market prices") from error

    app.router.add_post("/v1/ha/market-prices", receive)
