"""Independent public-wire validation and three-source recommendation regressions."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from itertools import combinations

import pytest

from open_energy_planner.prices.adapters.jeroen import JeroenPrices
from open_energy_planner.prices.model import PriceKind, PricesUnavailable
from open_energy_planner.prices.selection import ForecastPair, ForecastSelection, PublishedHour
from open_energy_planner.prices.source_selection import SourceSelection
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA

NOW = datetime(2026, 10, 27, 8, tzinfo=UTC)


def forecast_payload():
    """Keep a known row, a negative median and a real zero distinct on the wire."""
    return {
        "source": "jeroen.nl",
        "unit": "EUR/kWh",
        "resolution": "quarter",
        "forecast_made_at_utc": "2026-10-27T03:00:04Z",
        "columns": [
            "time_local",
            "known_market_price",
            "forecast_p10",
            "forecast_p50",
            "forecast_p90",
            "time_utc",
        ],
        "rows": [
            ["2026-10-27 09:00", 0.03, None, None, None, "2026-10-27T08:00:00Z"],
            ["2026-10-27 09:15", None, -0.03, -0.01, 0.02, "2026-10-27T08:15:00Z"],
            ["2026-10-27 09:30", None, -0.02, 0.0, 0.03, "2026-10-27T08:30:00Z"],
        ],
    }


class PublicMcp:
    """Record public RPC requests without provider access or application credentials."""

    def __init__(self, payload):
        """Own a detached fixture and request log."""
        self.payload, self.calls = payload, []

    async def post(self, url, body):
        """Reply to initialization and the one declared public forecast tool."""
        self.calls.append((url, body))
        if body["method"] == "initialize":
            return {"result": {"protocolVersion": "2025-03-26"}}
        return {"result": {"structuredContent": deepcopy(self.payload)}}


def test_jeroen_preserves_real_zero_negative_prices_issue_time_and_known_exclusion():
    """Published rows are excluded. Two forecast quarters retain -0.01 and 0.00 EUR/kWh."""
    client = PublicMcp(forecast_payload())
    series = asyncio.run(JeroenPrices(client, "NL").fetch(NOW))
    assert [point.value for point in series.points] == [-0.01, 0.0]
    assert all(
        point.kind == PriceKind.FORECAST and point.native_resolution_minutes == 15
        for point in series.points
    )
    assert series.issued_at == datetime(2026, 10, 27, 3, 0, 4, tzinfo=UTC)
    request = client.calls[-1][1]["params"]
    assert request["name"] == "get_electricity_price_forecast"
    assert request["arguments"] == {
        "days": 5,
        "resolution": "quarter",
        "include_consumer_price": False,
    }
    assert series.market_area == "NL" and series.currency == "EUR"


@pytest.mark.parametrize(
    "fault",
    [
        "future",
        "stale",
        "unit",
        "source",
        "band",
        "nan",
        "bool",
        "duplicate",
        "seconds",
        "missing_utc",
        "bad_rows",
    ],
)
def test_jeroen_rejects_malformed_or_stale_evidence(fault):
    """Do not turn malformed forecasts, misaligned times or old model runs into usable prices."""
    payload = forecast_payload()
    if fault in ("future", "stale"):
        payload["forecast_made_at_utc"] = (
            "2026-10-28T03:00:00Z" if fault == "future" else "2026-10-25T03:00:00Z"
        )
    elif fault in ("unit", "source"):
        payload[fault] = "unexpected"
    elif fault in ("band", "nan", "bool"):
        payload["rows"][1][3] = {"band": 1.0, "nan": float("nan"), "bool": True}[fault]
    elif fault == "duplicate":
        payload["rows"].append(payload["rows"][-1])
    elif fault == "seconds":
        payload["rows"][1][-1] = "2026-10-27T08:15:01Z"
    elif fault == "missing_utc":
        payload["columns"][-1] = "local_only"
    else:
        payload["rows"] = None
    with pytest.raises(PricesUnavailable, match="invalid_jeroen_forecast"):
        asyncio.run(JeroenPrices(PublicMcp(payload), "NL").fetch(NOW))


def test_jeroen_does_not_fetch_other_markets():
    """A Dutch-only source must not request or relabel prices for Belgium."""
    client = PublicMcp(forecast_payload())
    with pytest.raises(PricesUnavailable, match="market_area_unsupported"):
        asyncio.run(JeroenPrices(client, "BE").fetch(NOW))
    assert client.calls == []


def test_jeroen_uses_utc_during_both_autumn_clock_folds():
    """Repeated 02:00 wall times remain separate UTC quarters and missing quarters remain gaps."""
    payload = forecast_payload()
    payload["forecast_made_at_utc"] = "2026-10-24T23:00:00Z"
    payload["rows"] = [
        ["2026-10-25 02:00", None, -0.02, -0.01, 0.01, "2026-10-25T00:00:00Z"],
        ["2026-10-25 02:15", None, -0.01, 0.0, 0.01, "2026-10-25T00:15:00Z"],
        ["2026-10-25 02:00", None, 0.0, 0.01, 0.02, "2026-10-25T01:00:00Z"],
    ]
    now = datetime(2026, 10, 25, tzinfo=UTC)
    result = asyncio.run(JeroenPrices(PublicMcp(payload), "NL").fetch(now))
    assert [point.start for point in result.points] == [
        now,
        now + timedelta(minutes=15),
        now + timedelta(hours=1),
    ]
    assert result.points[1].end < result.points[2].start


def test_new_receipt_never_refreshes_an_old_daily_model():
    """A later HTTP receipt cannot extend the original daily model's admitted lifetime."""
    adapter = JeroenPrices(PublicMcp(forecast_payload()), "NL")
    first = asyncio.run(adapter.fetch(NOW))
    with pytest.raises(PricesUnavailable):
        asyncio.run(adapter.fetch(first.expires_at))


def make_selection(engine, area, *, defaults=None, source_horizons=None):
    """Share the real schema and namespace while scoping every source pair independently."""
    SCHEMA.create_all(engine)
    selectors = tuple(
        ForecastSelection(
            SqlAlchemySelectionStore(engine, sources=None if pair == ("first", "second") else pair),
            area,
            *pair,
        )
        for pair in combinations(("first", "second", "third"), 2)
    )
    return SourceSelection(
        selectors,
        SqlDocuments(engine, "prices"),
        defaults=defaults,
        source_horizons=source_horizons,
    )


def test_manual_source_horizons_reject_tail_choice_without_rewriting_saved_preferences(
    selection_store,
):
    """All backends retain legacy choices, reject a 48-hour source for the tail and save a supported replacement."""
    adapter, area = selection_store
    legacy = make_selection(adapter.engine, area)
    legacy.save_preferences(["first", "second", "first"], None)
    saved_document = legacy.documents.read(legacy.settings_name)
    selection = make_selection(
        adapter.engine, area, source_horizons={"first": 48, "second": 72, "third": 72}
    )
    assert selection.issue is None
    assert selection.manual == ("first", "second", "first")
    assert selection.configuration_version == saved_document["version"]
    with pytest.raises(ValueError, match="does not support"):
        selection.save_preferences(["third", None, "first"], selection.configuration_version)
    assert selection.documents.read(selection.settings_name) == saved_document
    assert selection.manual == ("first", "second", "first")
    selection.save_preferences(["first", "first", "third"], selection.configuration_version)
    assert selection.manual == ("first", "first", "third")


@pytest.mark.parametrize("result", [None, [], "unexpected", {"isError": True}])
def test_jeroen_rejects_invalid_rpc_results(result):
    """Malformed tool envelopes become a sanitized unavailable source, never an unexpected runtime error."""

    class InvalidRpc(PublicMcp):
        """Return valid initialization followed by an invalid tool result."""

        async def post(self, url, body):
            """Preserve initialization while varying only the tool result envelope."""
            if body["method"] == "initialize":
                return await super().post(url, body)
            return {"result": result}

    with pytest.raises(PricesUnavailable, match="invalid_jeroen_forecast"):
        asyncio.run(JeroenPrices(InvalidRpc(forecast_payload()), "NL").fetch(NOW))


@pytest.mark.parametrize(
    "fault", ["preference_hash", "negative_streak", "invalid_day", "unknown_challenger"]
)
def test_invalid_selection_documents_are_reported_without_rewriting_evidence(
    selection_store, fault
):
    """Corrupt control documents remain untouched and cannot become trusted preferences or streaks."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    if fault == "preference_hash":
        name = selection.settings_name
        document = {"preferences": ["third", None, None], "version": "invalid"}
    else:
        name = selection.state_name
        state = {
            "preferred_source": "first",
            "streak": 1,
            "assessed_on": "2026-10-27",
            "challenger": "third",
        }
        state.update(
            {
                "negative_streak": {"streak": -1},
                "invalid_day": {"assessed_on": "unknown"},
                "unknown_challenger": {"challenger": "unknown"},
            }[fault]
        )
        document = {"1": state}
    selection.documents.write(name, document)
    restored = make_selection(adapter.engine, area)
    assert restored.issue == "forecast_selection_storage_unavailable"
    assert restored.manual == (None, None, None)
    assert restored.documents.read(name) == document


def seed_quality(selection):
    """Seven zero-price days make source errors exactly 0.02, 0.015 and 0.005 EUR/kWh."""
    errors = {"first": 0.02, "second": 0.015, "third": 0.005}
    targets = tuple(
        NOW.replace(hour=0) - timedelta(days=7) + timedelta(hours=hour) for hour in range(168)
    )
    actuals = tuple(PublishedHour(target, 0.0, "a" * 64) for target in targets)
    for pair in selection.comparisons:
        samples = tuple(
            ForecastPair(
                1,
                target - timedelta(hours=30),
                target,
                errors[pair.primary],
                errors[pair.alternative],
                "b" * 64,
                "c" * 64,
            )
            for target in targets
        )
        pair.store.update(
            selection.area,
            samples,
            (),
            NOW - timedelta(days=8),
            NOW - timedelta(days=38),
            pair.decide,
        )
    for pair in selection.comparisons:
        pair.store.update(selection.area, (), actuals, NOW, NOW - timedelta(days=30), pair.decide)


def test_retired_legacy_preference_does_not_enter_three_source_decisions(selection_store):
    """Ignore a retired provider in old pair state without deleting the historical decision."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    seed_quality(selection)
    table = selection.comparisons[0].store.decisions
    with adapter.engine.begin() as connection:
        connection.execute(table.update().where(table.c.area == area).values(preferred="retired"))
    restored = make_selection(adapter.engine, area)
    assert restored.preferred == ("first", "first", "first")
    restored.states = restored.update((), NOW)
    assert restored.preferred[1] == "first"
    assert restored.state(1)["challenger"] == "third"


def test_unproven_legacy_tail_default_cannot_block_the_covered_source_comparison(selection_store):
    """Keep 27 old matched hours as history, but start tail selection from the covered default rather than an unproven legacy preference."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    seed_quality(selection)
    table = selection.comparisons[0].store.decisions
    with adapter.engine.begin() as connection:
        connection.execute(
            table.update().where(table.c.area == area, table.c.bucket == 2).values(hours=27, days=2)
        )
    restored = make_selection(adapter.engine, area, defaults=("first", "first", "second"))
    assert restored.preferred[2] == "second"
    assert restored.comparisons[0].store.load(area)[2]["matched_hours"] == 27
    assert restored.comparisons[0].store.load(area)[2]["preferred_source"] == "first"


def test_three_source_selection_switches_after_three_days_and_manual_choice_survives_restart(
    selection_store,
):
    """All four backends pick the 0.005-error challenger only on day three and retain explicit manual preferences."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    seed_quality(selection)
    for day in range(3):
        selection.states = selection.update((), NOW + timedelta(days=day))
        assert selection.preferred[1] == ("first" if day < 2 else "third")
    assert selection.state(1)["recommended_source"] == "third"
    comparisons = selection.state(1)["comparisons"]
    third_pair = next(
        pair
        for pair in comparisons
        if pair["primary_source"] == "first" and pair["alternative_source"] == "third"
    )
    assert third_pair["primary_mae"] == pytest.approx(0.02)
    assert third_pair["alternative_mae"] == pytest.approx(0.005)
    assert third_pair["matched_hours"] == 168
    selection.save_preferences([None, "second", None], None)
    restored = make_selection(adapter.engine, area)
    assert restored.preferred[1] == "second"
    assert restored.state(1)["recommended_source"] == "third"
    assert restored.state(1)["selection_mode"] == "manual"
    with pytest.raises(ValueError, match="changed"):
        restored.save_preferences([None, None, None], None)
    with pytest.raises(ValueError, match="available"):
        restored.save_preferences([None, "unknown", None], restored.configuration_version)


def test_manual_selection_does_not_stop_quality_collection(selection_store):
    """A manual first-source preference keeps collecting evidence and recommends the independently better third source."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    selection.save_preferences(["first", "first", "first"], None)
    seed_quality(selection)
    for day in range(3):
        selection.states = selection.update((), NOW + timedelta(days=day))
    assert selection.preferred[1] == "first"
    assert selection.state(1)["recommended_source"] == "third"


def test_valuation_bucket_compares_only_sources_with_tail_coverage(selection_store):
    """An uncovered first-source default must not prevent the second/third comparison at 48-72 hours."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    selection.defaults = ("first", "first", "second")
    seed_quality(selection)
    for pair in selection.comparisons:
        with adapter.engine.begin() as connection:
            table = pair.store.pairs
            connection.execute(
                table.update()
                .where(table.c.area == area, *pair.store._scope(table))
                .values(bucket=2)
            )
            decisions = pair.store.decisions
            connection.execute(
                decisions.delete().where(decisions.c.area == area, *pair.store._scope(decisions))
            )
    for day in range(3):
        selection.states = selection.update((), NOW + timedelta(days=day))
    assert selection.preferred[2] == "third"
    state = selection.state(2)
    assert (state["primary_source"], state["alternative_source"]) == ("second", "third")
    assert (state["primary_mae"], state["alternative_mae"]) == pytest.approx((0.015, 0.005))
    selection.states = selection.update((), NOW + timedelta(days=3))
    retained = selection.state(2)
    assert retained["reason"] == "retained_within_threshold"
    errors = {"first": 0.02, "second": 0.015, "third": 0.005}
    assert retained["primary_mae"] == pytest.approx(errors[retained["primary_source"]])
    assert retained["alternative_mae"] == pytest.approx(errors[retained["alternative_source"]])


def test_manual_source_respects_publication_and_falls_back_on_partial_coverage(selection_store):
    """A manual third choice cannot replace published prices and uses another forecast for its missing hours."""
    from open_energy_planner.prices.service import PriceService
    from open_energy_planner.prices.storage.adapters.store import PriceStore
    from tests.app.price.test_price_selection import series

    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    selection.save_preferences(["third", "third", "third"], None)
    service = PriceService(
        area, None, None, PriceStore(SqlDocuments(adapter.engine, "prices")), selection=selection
    )
    service.series = (
        series("first", start=NOW, receipt=NOW, area=area),
        series("third", start=NOW, receipt=NOW, hours=1, area=area),
        series("market", start=NOW, receipt=NOW, hours=1, area=area, kind=PriceKind.PUBLISHED),
    )
    timeline = service.timeline(NOW)
    assert all(entry.source_id == "market" for entry in timeline.entries[:4])
    assert all(entry.source_id == "first" for entry in timeline.entries[4:])
    row = service.document(NOW)["forecast_selection"][0]
    assert row["preferred_source"] == "third"
    assert row["selection_mode"] == "manual"
    assert row["usage_reason"] == "available_source_fallback"
    assert row["published_prices_used"] is True
    later = service.document(NOW + timedelta(hours=2))
    assert (
        next(source for source in later["source_status"] if source["source_id"] == "third")[
            "available"
        ]
        is False
    )


def test_failed_preference_transaction_preserves_previous_memory_and_durable_value(
    selection_store, monkeypatch
):
    """A pointer-write failure rolls back its revision and retains the previously confirmed preference."""
    adapter, area = selection_store
    selection = make_selection(adapter.engine, area)
    selection.save_preferences(["first", None, None], None)
    version = selection.configuration_version
    original_write = selection.documents.write

    def fail_pointer(name, document):
        """Fail after writing the immutable revision but before committing its mutable pointer."""
        if name == selection.settings_name:
            raise OSError("storage unavailable")
        original_write(name, document)

    monkeypatch.setattr(selection.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        selection.save_preferences(["third", None, None], version)
    restored = make_selection(adapter.engine, area)
    assert restored.manual == selection.manual == ("first", None, None)
    assert restored.configuration_version == selection.configuration_version == version


def test_price_preference_http_requires_authentication_and_current_area_revision(
    tmp_path, monkeypatch
):
    """The API rejects invalid edits, retains failed writes and stays responsive while source refresh blocks a preference save."""
    from contextlib import asynccontextmanager

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from open_energy_planner.prices.runtime import ConfiguredPrices
    from open_energy_planner.prices.service import PriceService
    from open_energy_planner.prices.storage.adapters.store import PriceStore

    token = "isolated-price-test-token-at-least-24-chars"
    state = {}

    @asynccontextmanager
    async def factory(engine):
        """Use real runtime ownership and durable selection while omitting all external providers."""

        async def read_area():
            """Keep the fixture's configured market explicit."""
            return "NL", None

        @asynccontextmanager
        async def opened(area):
            """Borrow the app engine with source-neutral fixture provider identities."""
            selection = make_selection(engine, area)
            state["selection"] = selection
            yield PriceService(
                area, None, None, PriceStore(SqlDocuments(engine, "prices")), selection=selection
            )

        runtime = ConfiguredPrices(read_area, opened)
        state["runtime"] = runtime
        try:
            await runtime.configure()
            yield runtime
        finally:
            await runtime.close()

    async def scenario():
        """Exercise authorization, optimistic concurrency, strict validation and durable write failure over HTTP."""
        async with TestClient(
            TestServer(create_app(tmp_path, token, price_factory=factory, source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer " + token}
            payload = {
                "preferences": [None, "third", None],
                "configuration_version": None,
                "market_area": "NL",
            }
            assert (await client.put("/v1/prices/preferences", json=payload)).status == 401
            for invalid in (
                {**payload, "market_area": "BE"},
                {**payload, "preferences": ["third"]},
                {**payload, "preferences": [None, "unknown", None]},
                {**payload, "extra": True},
            ):
                assert (
                    await client.put("/v1/prices/preferences", headers=headers, json=invalid)
                ).status == 400
            response = await client.put("/v1/prices/preferences", headers=headers, json=payload)
            assert response.status == 200
            document = await response.json()
            saved = document["source_preferences"]
            assert saved["preferences"] == [None, "third", None]
            assert saved["configuration_version"]
            assert (
                await client.put("/v1/prices/preferences", headers=headers, json=payload)
            ).status == 400

            def unavailable(*_args):
                """An unavailable writer must never acknowledge an unsaved revision."""
                raise OSError("private database details must stay private")

            original_save = state["selection"].save_preferences
            monkeypatch.setattr(state["selection"], "save_preferences", unavailable)
            response = await client.put(
                "/v1/prices/preferences",
                headers=headers,
                json={
                    **payload,
                    "configuration_version": saved["configuration_version"],
                    "preferences": [None, None, None],
                },
            )
            assert response.status == 503
            assert "private database" not in await response.text()
            retained = await (await client.get("/v1/prices", headers=headers)).json()
            assert retained["source_preferences"] == saved
            monkeypatch.setattr(state["selection"], "save_preferences", original_save)
            runtime = state["runtime"]
            original_request = runtime.save_preferences
            waiting = asyncio.Event()

            async def waiting_preferences(preferences, version, area):
                """Observe handler entry before the real runtime waits for its in-progress source refresh."""
                waiting.set()
                await original_request(preferences, version, area)

            monkeypatch.setattr(runtime, "save_preferences", waiting_preferences)
            pending = None
            try:
                async with runtime.lock:
                    pending = asyncio.create_task(
                        client.put(
                            "/v1/prices/preferences",
                            headers=headers,
                            json={
                                **payload,
                                "configuration_version": saved["configuration_version"],
                            },
                        )
                    )
                    await asyncio.wait_for(waiting.wait(), 2)
                    response = await asyncio.wait_for(
                        client.get("/v1/strategy", headers=headers), 2
                    )
                    assert response.status == 200
            finally:
                if pending is not None:
                    assert (await pending).status == 200

    asyncio.run(scenario())
