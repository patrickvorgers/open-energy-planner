"""Own the configured price service without exposing provider or database details."""

import asyncio
from contextlib import AsyncExitStack

from .model import PricesUnavailable


class ConfiguredPrices:
    """Switch complete source services when the saved market area changes.

    Cached typed series remain read-only outside the serialized refresh task.
    A replacement is opened before the old resources close. Missing/corrupt
    configuration never selects a default area or exposes incompatible prices.
    The composition root owns provider factories and durable configuration reads.
    """

    def __init__(self, read_area, open_service):
        """Borrow an async configuration reader and an area-specific context factory."""
        self.read_area, self.open_service = read_area, open_service
        self.service = None
        self.market_area = None
        self.configuration_issue = None
        self.resources = AsyncExitStack()
        self.lock = asyncio.Lock()
        self._persistence_lock = asyncio.Lock()

    @property
    def persistence_lock(self):
        """Return the shared financial-publication barrier, including before configuration."""
        return self._persistence_lock

    @persistence_lock.setter
    def persistence_lock(self, value):
        """Pass the app-owned barrier to current and subsequently opened services."""
        self._persistence_lock = value
        if self.service is not None:
            self.service.persistence_lock = value

    @property
    def series(self):
        """Expose only prices admitted for readable current configuration."""
        return self.service.series if self.service and not self.configuration_issue else ()

    @property
    def version(self):
        """Keep the source version unavailable until valid configuration selects a service."""
        return self.service.version if self.series else None

    @property
    def issues(self):
        """Retain configuration faults independently from ordinary provider issues."""
        return {
            **(self.service.issues if self.service else {}),
            **({"configuration": self.configuration_issue} if self.configuration_issue else {}),
        }

    def supports_identity(self, market_area, currency):
        """Allow explicit EUR bidding areas. Providers must still prove actual coverage."""
        return (
            isinstance(market_area, str)
            and bool(market_area.strip())
            and market_area == market_area.strip()
            and currency == "EUR"
        )

    async def configure(self):
        """Restore or replace the complete service without fetching weather or market data."""
        area, issue = await self.read_area()
        self.configuration_issue = issue
        if issue or area == self.market_area:
            return False
        if area is None:
            previous = self.resources
            self.service, self.market_area, self.resources = None, None, AsyncExitStack()
            await previous.aclose()
            return True
        self.configuration_issue = "market_area_loading"
        replacement = AsyncExitStack()
        try:
            service = await replacement.enter_async_context(self.open_service(area))
            service.persistence_lock = self.persistence_lock
        except BaseException:
            self.configuration_issue = "market_area_reload_failed"
            await replacement.aclose()
            raise
        old = self.resources
        self.service, self.market_area, self.resources = service, area, replacement
        self.configuration_issue = None
        await old.aclose()
        return True

    async def refresh(self, now):
        """Apply the current saved area and run its existing independent fetch schedule."""
        async with self.lock:
            changed = await self.configure()
            if self.service is None or self.configuration_issue:
                return changed
            return await self.service.refresh(now) or changed

    def timeline(self, now, **options):
        """Delegate typed price composition only after configuration has been admitted."""
        if self.service is None or self.configuration_issue:
            raise PricesUnavailable("market_prices_not_configured")
        return self.service.timeline(now, **options)

    def document(self, now):
        """Publish setup readiness even before the first contract has been entered."""
        if self.service is None or self.configuration_issue:
            return {
                "status": "unavailable" if self.configuration_issue else "not_configured",
                "market_area": self.market_area,
                "currency": "EUR",
                "issues": self.issues,
            }
        return self.service.document(now)

    async def save_preferences(self, preferences, expected_version, expected_area):
        """Serialize preference writes with source refresh and keep its owned writer alive."""
        async with self.lock:
            await self.configure()
            if self.service is None or self.configuration_issue:
                raise ValueError("Configure the market area before selecting price sources")
            if expected_area != self.market_area:
                raise ValueError("Market area changed. Reload before saving price preferences")
            async with self.persistence_lock:
                from ..storage.writes import complete_write

                cancellation = await complete_write(
                    self.service.selection.save_preferences, preferences, expected_version
                )
                if cancellation is not None:
                    raise cancellation

    async def close(self):
        """Release the currently selected HTTP/source resources before the engine closes."""
        await self.resources.aclose()
