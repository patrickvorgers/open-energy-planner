"""Market capability admission across composition, adapter requests and durable preferences."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from open_energy_planner.prices.adapters.energy_price_forecast import EnergyPriceForecast
from open_energy_planner.prices.adapters.epex_predictor import EpexPredictor
from open_energy_planner.prices.adapters.jeroen import JeroenPrices
from open_energy_planner.prices.bootstrap import price_factory
from open_energy_planner.prices.model import PricesUnavailable
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.source_selection import SourceSelection
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.app.price.test_prices import forecast
from tests.database.contracts.test_jeroen_prices import make_selection

NOW = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.mark.parametrize("source_ids", [(), ("first",), ("first", "second")])
@pytest.mark.parametrize("corrupted_document", ["preferences", "recommendation"])
def test_assessment_retains_restore_failure_until_successful_reload(
    selection_store, source_ids, corrupted_document
):
    """No market can hide an initial restoration failure or overwrite its recovery evidence."""
    store, area = selection_store
    original = make_selection(store.engine, area)
    original.save_preferences(["first", None, None], None)
    original.documents.write(
        original.state_name,
        {
            "0": {
                "preferred_source": "first",
                "challenger": None,
                "streak": 0,
                "assessed_on": "2026-10-26",
            }
        },
    )
    failed_name = (
        original.settings_name if corrupted_document == "preferences" else original.state_name
    )
    saved = original.documents.read(failed_name)
    damaged = (
        {**saved, "version": "corrupt"}
        if corrupted_document == "preferences"
        else "unreadable-decision"
    )
    original.documents.write(failed_name, damaged)
    comparisons = original.comparisons[:1] if len(source_ids) > 1 else ()
    selection = SourceSelection(
        comparisons,
        original.documents,
        market_area=area,
        sources=source_ids,
        known_sources=("first", "second", "third"),
    )
    service = PriceService(area, None, None, PriceStore(original.documents), selection=selection)
    asyncio.run(service.assess(NOW))
    assert selection.issue == "forecast_selection_storage_unavailable"
    assert service.document(NOW)["forecast_selection"][0]["selection_issue"] == selection.issue
    assert original.documents.read(failed_name) == damaged
    original.documents.write(failed_name, saved)
    restored = SourceSelection(
        comparisons,
        original.documents,
        market_area=area,
        sources=source_ids,
        known_sources=("first", "second", "third"),
    )
    assert restored.issue is None
    assert restored.manual == ("first", None, None)


def test_temporary_assessment_write_failure_recovers_without_restart(selection_store, monkeypatch):
    """A later write failure retries normally instead of being mistaken for an initial restore failure."""
    store, area = selection_store
    selection = make_selection(store.engine, area)
    service = PriceService(area, None, None, PriceStore(selection.documents), selection=selection)
    original_write = selection.documents.write

    def unavailable(*args):
        """Fail only the global assessment document write, leaving its transaction to roll back."""
        raise OSError("Temporary assessment write unavailable")

    monkeypatch.setattr(selection.documents, "write", unavailable)
    asyncio.run(service.assess(NOW))
    assert selection.issue == "Temporary assessment write unavailable"
    assert not selection.documents.exists(selection.state_name)
    monkeypatch.setattr(selection.documents, "write", original_write)
    asyncio.run(service.assess(NOW))
    assert selection.issue is None
    assert "selection" not in service.issues
    assert set(selection.documents.read(selection.state_name)) == {"0", "1", "2"}


def test_comparison_restoration_failure_cannot_be_cleared_by_assessment(
    selection_store, monkeypatch
):
    """An initial pair-history read failure also remains visible until successful reload."""
    store, area = selection_store

    def unavailable(*args):
        """Reject initial pair-history reads without changing the underlying database."""
        raise OSError("Comparison history unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(type(store), "load", unavailable)
        selection = make_selection(store.engine, area)
    service = PriceService(area, None, None, PriceStore(selection.documents), selection=selection)
    asyncio.run(service.assess(NOW))
    assert selection.issue == "forecast_selection_storage_unavailable"
    assert not selection.documents.exists(selection.state_name)
    reloaded = make_selection(store.engine, area)
    assert reloaded.issue is None


@pytest.mark.parametrize(
    "source_ids,winner", [(("first",), "first"), (("first", "second"), "second")]
)
def test_eligible_recommendation_survives_removed_comparison_without_stale_scores(
    selection_store, source_ids, winner
):
    """Keep the eligible winner while hiding obsolete pair identities, error scores and streaks."""
    store, area = selection_store
    original = make_selection(store.engine, area)
    saved = {
        "0": {
            "preferred_source": winner,
            "challenger": "third",
            "streak": 2,
            "primary_source": winner,
            "alternative_source": "third",
            "primary_mae": 0.02,
            "alternative_mae": 0.01,
            "matched_hours": 168,
            "settled_days": 7,
            "assessed_on": "2026-10-26",
        }
    }
    original.documents.write(original.state_name, saved)
    restricted = SourceSelection(
        original.comparisons[:1] if len(source_ids) > 1 else (),
        original.documents,
        market_area=area,
        sources=source_ids,
        known_sources=("first", "second", "third"),
    )
    assert restricted.issue is None
    assert restricted.preferred[0] == winner
    state = restricted.state(0)
    assert state["recommended_source"] == winner
    assert state["selection_mode"] == "automatic"
    assert set(state) == {"preferred_source", "recommended_source", "selection_mode", "comparisons"}
    assert len(state["comparisons"]) == len(source_ids) - 1
    assert original.documents.read(original.state_name) == saved


@pytest.mark.parametrize(
    "market,expected_source_ids",
    [
        ("NL", ["energy_price_forecast_eu", "epex_predictor", "jeroen_nl"]),
        ("BE", ["energy_price_forecast_eu", "epex_predictor"]),
        ("NO1", ["energy_price_forecast_eu"]),
        ("FI", ["energy_price_forecast_eu"]),
        ("HU", []),
    ],
)
def test_market_composition_exposes_only_supported_sources(
    selection_store, market, expected_source_ids
):
    """Unsupported providers are absent from service ports and API choices without any network request."""
    store, _ = selection_store
    SCHEMA.create_all(store.engine)

    async def scenario():
        """Open real adapter composition and inspect its executable selection boundary."""
        async with price_factory(market)(store.engine) as service:
            document = service.document(NOW)
            assert service.selection.issue is None
            assert document["source_preferences"]["available_sources"] == expected_source_ids
            assert [
                adapter.SOURCE_ID
                for adapter in (service.forecast, service.alternative, *service.extra_forecasts)
                if adapter is not None
            ] == expected_source_ids
            assert (
                len(service.selection.comparisons)
                == len(expected_source_ids) * (len(expected_source_ids) - 1) // 2
            )
            assert service.selection.validate([None, None, None]) == (None, None, None)
            if "jeroen_nl" not in expected_source_ids:
                with pytest.raises(ValueError, match="available forecast source"):
                    service.selection.save_preferences(["jeroen_nl", None, None], None)
            if len(expected_source_ids) < 2:
                assert service.selection.update((), NOW) == service.selection.states
            if len(expected_source_ids) == 1:
                assert service.selection.preferred == (
                    "energy_price_forecast_eu",
                    "energy_price_forecast_eu",
                    None,
                )
                with pytest.raises(ValueError, match="does not support"):
                    service.selection.validate([None, None, "energy_price_forecast_eu"])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "adapter,market",
    [
        (JeroenPrices, "BE"),
        (EpexPredictor, "NO1"),
        (EnergyPriceForecast, "HU"),
    ],
)
def test_unsupported_adapter_market_never_calls_transport(adapter, market):
    """Direct adapter use also rejects an unsupported market before accessing the client."""
    with pytest.raises(PricesUnavailable, match="market_area_unsupported"):
        asyncio.run(adapter(None, market).fetch(NOW))


@pytest.mark.parametrize("replacement", [None, "first"])
def test_removed_market_source_preserves_revision_and_restores_when_eligible(
    selection_store, replacement
):
    """Unsupported stored choices become inactive without overwriting preferences or immutable history."""
    store, area = selection_store
    original = make_selection(store.engine, area)
    original.save_preferences(["third", "first", None], None)
    saved = original.documents.read(original.settings_name)
    restricted = SourceSelection(
        (),
        SqlDocuments(store.engine, "prices"),
        market_area=area,
        sources=("first",),
        known_sources=("first", "second", "third"),
        source_horizons={"first": 48},
        defaults=("first", "first", None),
    )
    assert restricted.issue is None
    assert restricted.manual == ("third", "first", None)
    assert restricted.active_manual == (None, "first", None)
    assert restricted.preferred == ("first", "first", None)
    assert restricted.configuration_version == saved["version"]
    assert restricted.documents.read(restricted.settings_name) == saved
    assert restricted.documents.read(f"source-preferences-{saved['version']}.json") == saved
    with pytest.raises(ValueError, match="available forecast source"):
        restricted.save_preferences([None, "third", None], saved["version"])
    assert make_selection(store.engine, area).manual == ("third", "first", None)
    document = PriceService(
        area, None, None, PriceStore(restricted.documents), selection=restricted
    ).document(NOW)
    assert document["source_preferences"]["preferences"] == [None, "first", None]
    assert document["source_preferences"]["stored_preferences"] == ["third", "first", None]
    restricted.save_preferences(["third", None, None], saved["version"])
    assert restricted.manual == ("third", None, None)
    assert restricted.active_manual == (None, None, None)
    assert make_selection(store.engine, area).manual == ("third", None, None)
    with pytest.raises(ValueError, match="changed. Reload"):
        restricted.save_preferences([replacement, None, None], saved["version"])
    restricted.save_preferences([replacement, None, None], restricted.configuration_version)
    assert restricted.manual == (replacement, None, None)
    assert restricted.documents.read(f"source-preferences-{saved['version']}.json") == saved


@pytest.mark.parametrize("wrong_area", [False, True])
def test_cached_unsupported_source_is_detached_without_deleting_archive(
    selection_store, wrong_area
):
    """Detach an ineligible forecast without a false issue, retaining real identity failures."""
    store, area = selection_store
    SCHEMA.create_all(store.engine)
    documents = SqlDocuments(store.engine, "prices")
    selection = SourceSelection(
        (),
        documents,
        market_area=area,
        sources=("first",),
        source_horizons={"first": 48},
    )
    prices = PriceStore(documents)
    previous = prices.load()
    ineligible_forecast = replace(
        forecast(), market_area="wrong" if wrong_area else area, source_id="removed"
    )
    prices.save((ineligible_forecast,))
    archived = selection.documents.read(f"{ineligible_forecast.version}.json")
    try:
        service = PriceService(area, None, None, prices, selection=selection)
        assert service.series == ()
        assert service.issues == ({"cache": "cache_identity_mismatch"} if wrong_area else {})
        assert service.document(NOW)["source_status"] == []
        assert selection.documents.read(f"{ineligible_forecast.version}.json") == archived
        assert prices.load() == (ineligible_forecast,)
    finally:
        prices.save(previous)
