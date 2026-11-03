"""Read supported HA market-price actions without provider HTTP or private coordinator access."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientResponseError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

PRICE_RETRY = timedelta(minutes=30)
ACTION_TIMEOUT_SECONDS = 25
UNKNOWN_AVAILABILITY_FETCH_TIME = time(14, 5)
MAX_REQUEST_DAYS = 2
_LOGGER = logging.getLogger(__name__)


class MarketPriceBridge:
    """Translate app day requests to supported public HA actions with bounded retries.

    A registered provider is selected only when loaded and compatible. Nord Pool
    publishes availability through its diagnostic binary sensor. Prefer that
    signal over a publication-time guess. If the diagnostic entity is disabled,
    allow requests after 14:05 local market time, allowing an hourly HA refresh
    after the usual 13:00 publication. This is a fallback, not a publication SLA.
    """

    def __init__(self, hass):
        """Keep action attempts and undelivered replies local to this HA connection."""
        self.hass = hass
        self.attempts = {}
        self.replies = {}
        self.identity = None
        self.task = None
        self.closed = False

    def start(self, request, client):
        """Run optional provider work independently of command publication and polling."""
        if not self.closed and request is not None and (self.task is None or self.task.done()):
            self.task = self.hass.async_create_background_task(
                self.transfer(request, client), "OEP market prices"
            )

    async def transfer(self, request, client):
        """Keep retryable replies and retire acknowledged or permanently rejected payloads."""
        try:
            report = await self.capture(request, datetime.now(ZoneInfo("Europe/Oslo")))
            await client.request("POST", "/v1/ha/market-prices", report)
            self.replies.clear()
        except ClientResponseError as error:
            if error.status in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.CONFLICT,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            ):
                self.replies.clear()
            _LOGGER.warning("Market price report rejected. Retrying within the provider interval")
        except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
            _LOGGER.warning("Market price transfer failed. Cached prices remain available")

    async def close(self):
        """Cancel and drain owned provider work before unloading the integration."""
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def capture(self, request, now):
        """Discover Nord Pool and return an explicit report without unrequested network calls."""
        if not isinstance(request, dict) or set(request) != {"market_area", "currency", "days"}:
            raise ValueError("Invalid market price request")
        area, currency = request["market_area"], request["currency"]
        days = request["days"]
        if not isinstance(area, str) or not area or currency != "EUR":
            raise ValueError("Invalid market price identity")
        if not isinstance(days, list) or len(days) > MAX_REQUEST_DAYS:
            raise ValueError("Invalid market price days")
        requests = {}
        for day_request in days:
            if (
                not isinstance(day_request, dict)
                or set(day_request) != {"date", "request_id"}
                or not isinstance(day_request["request_id"], str)
                or len(day_request["request_id"]) != 32
            ):
                raise ValueError("Invalid market price day request")
            requested_day = date.fromisoformat(day_request["date"])
            if requested_day in requests:
                raise ValueError("Duplicate market price day")
            requests[requested_day] = day_request["request_id"]
        days = tuple(requests)
        local = now.astimezone(ZoneInfo("Europe/Oslo"))
        if any(not 0 <= (day - local.date()).days <= 1 for day in days):
            raise ValueError("Market price days outside the supported window")
        entries = self.hass.config_entries.async_entries("nordpool")
        compatible = [
            entry
            for entry in entries
            if (area in entry.data.get("areas", ()) and entry.data.get("currency") == currency)
        ]
        loaded = [entry for entry in compatible if entry.state == ConfigEntryState.LOADED]
        available = len(loaded) == 1 and self.hass.services.has_service(
            "nordpool", "get_prices_for_date"
        )
        reason = (
            "ready"
            if available
            else "not_configured"
            if not entries
            else "incompatible"
            if not compatible
            else "ambiguous"
            if len(loaded) > 1
            else "not_loaded"
        )
        identity = (area, currency, loaded[0].entry_id if available else None)
        if identity != self.identity:
            self.attempts, self.replies, self.identity = {}, {}, identity
        self.replies = {
            day: reply
            for day, reply in self.replies.items()
            if day in requests and reply["request_id"] == requests[day]
        }
        self.attempts = {
            day: attempt for day, attempt in self.attempts.items() if day >= local.date()
        }
        if available:
            registry = er.async_get(self.hass)
            sensor = registry.async_get_entity_id(
                "binary_sensor", "nordpool", f"{area}-tomorrow_price_available"
            )
            availability = self.hass.states.get(sensor) if sensor else None
            for day in days:
                if day in self.replies:
                    continue
                if day > local.date() and (
                    availability is not None
                    and availability.state != "on"
                    or availability is None
                    and local.time() < UNKNOWN_AVAILABILITY_FETCH_TIME
                ):
                    reason = "waiting_for_publication"
                    continue
                previous = self.attempts.get(day)
                if previous is not None and now - previous < PRICE_RETRY:
                    reason = "request_failed"
                    continue
                self.attempts[day] = now
                try:
                    async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                        response = await self.hass.services.async_call(
                            "nordpool",
                            "get_prices_for_date",
                            {
                                "config_entry": loaded[0].entry_id,
                                "date": day.isoformat(),
                                "areas": [area],
                                "currency": currency,
                            },
                            blocking=True,
                            return_response=True,
                        )
                    if not isinstance(response, dict) or not response.get(area):
                        reason = "waiting_for_publication"
                        continue
                    self.replies[day] = {
                        "date": day.isoformat(),
                        "request_id": requests[day],
                        "data": response,
                    }
                except (HomeAssistantError, TimeoutError):
                    reason = "request_failed"
        return {
            "market_area": area,
            "currency": currency,
            "available": available,
            "reason": reason,
            "responses": list(self.replies.values()),
        }
