"""Composition root selecting price adapters. Application/domain code stays provider-neutral."""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from itertools import combinations

from aiohttp import ClientSession

from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.prices.adapters.energy_price_forecast import EnergyPriceForecast
from open_energy_planner.prices.adapters.epex_predictor import EpexPredictor
from open_energy_planner.prices.adapters.home_assistant import AutomaticPublishedPrices
from open_energy_planner.prices.adapters.jeroen import JeroenPrices
from open_energy_planner.prices.adapters.json_client import JsonClient
from open_energy_planner.prices.adapters.nord_pool import NordPoolPrices
from open_energy_planner.prices.policy import LEAD_BUCKET_COUNT, LEAD_BUCKET_HOURS
from open_energy_planner.prices.runtime import ConfiguredPrices
from open_energy_planner.prices.selection import ForecastSelection
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.source_selection import SourceSelection
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments


def price_factory(market_area, *, home_assistant=False):
    """Build available market adapters and their shared automatic/manual selection policy.

    Args:
        market_area: Bidding area for published prices and compatible forecasts.
        home_assistant: Wrap Nord Pool in AutomaticPublishedPrices when true.
            The default False retains direct retrieval. configured_price_factory enables
            the automatic HA route by default.
    """

    @asynccontextmanager
    async def opened(database_engine):
        """Own the HTTP session and yield one source-neutral cached price service."""
        async with ClientSession() as session:
            client = JsonClient(session)
            adapters = (EnergyPriceForecast, EpexPredictor, JeroenPrices)
            forecasts = [
                adapter(client, market_area)
                for adapter in adapters
                if market_area in adapter.SUPPORTED_MARKETS
            ]
            sources = [forecast.SOURCE_ID for forecast in forecasts]
            comparisons = tuple(
                ForecastSelection(
                    SqlAlchemySelectionStore(
                        database_engine,
                        sources=None
                        if pair == (EnergyPriceForecast.SOURCE_ID, EpexPredictor.SOURCE_ID)
                        else pair,
                    ),
                    market_area,
                    *pair,
                )
                for pair in combinations(sources, 2)
            )
            selection = SourceSelection(
                comparisons,
                SqlDocuments(database_engine, "prices"),
                market_area=market_area,
                sources=sources,
                known_sources=tuple(adapter.SOURCE_ID for adapter in adapters),
                source_horizons={
                    forecast.SOURCE_ID: forecast.HORIZON_HOURS for forecast in forecasts
                },
                defaults=tuple(
                    next(
                        (
                            forecast.SOURCE_ID
                            for forecast in forecasts
                            if (bucket + 1) * LEAD_BUCKET_HOURS <= forecast.HORIZON_HOURS
                        ),
                        None,
                    )
                    for bucket in range(LEAD_BUCKET_COUNT)
                ),
            )
            published = NordPoolPrices(client, market_area)
            if home_assistant:
                published = AutomaticPublishedPrices(market_area, published)
            yield PriceService(
                market_area,
                published,
                forecasts[0] if forecasts else None,
                PriceStore(SqlDocuments(database_engine, "prices")),
                alternative=forecasts[1] if len(forecasts) > 1 else None,
                extra_forecasts=tuple(forecasts[2:]),
                selection=selection,
            )

    return opened


def configured_price_factory(factory=None):
    """Read the app-owned contract area, preserving the normal adapter factory seam."""
    factory = factory or partial(price_factory, home_assistant=True)

    @asynccontextmanager
    async def opened(database_engine):
        """Keep the app usable without a contract, then select sources automatically."""

        def read_area():
            """Read detached SQL configuration without provider calls or default-country guesses."""
            store = ContractStore(SqlDocuments(database_engine, "contracts"))
            return (store.current.market_area if store.current else None), store.issue

        async def configured_area():
            """Own the off-loop read through repeated cancellation before releasing its engine."""
            worker = asyncio.create_task(asyncio.to_thread(read_area))
            cancellation = None
            while True:
                try:
                    result = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError as error:
                    cancellation = error
            if cancellation is not None:
                raise cancellation
            return result

        runtime = ConfiguredPrices(configured_area, lambda area: factory(area)(database_engine))
        try:
            await runtime.configure()
            yield runtime
        finally:
            await runtime.close()

    return opened
