"""Independent paired-error arithmetic, source selection and provider-boundary scenarios."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.prices.adapters.epex_predictor import EpexPredictor
from open_energy_planner.prices.model import (
    PriceKind,
    PricePoint,
    PriceSeries,
    PricesUnavailable,
    combine,
)
from open_energy_planner.prices.selection import ForecastSelection, hourly
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.prices.storage.adapters.store import PriceStore
from tests.database.support.archive import make_store

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def series(
    source, start=NOW, value=0.02, hours=72, kind=PriceKind.FORECAST, receipt=NOW, area="NL"
):
    """Build immutable hourly wholesale observations with distinct provider identities."""
    return PriceSeries(
        source,
        area,
        "EUR",
        receipt,
        receipt,
        receipt + timedelta(hours=6) if kind == PriceKind.FORECAST else None,
        tuple(
            PricePoint(
                start + timedelta(hours=index), start + timedelta(hours=index + 1), value, kind, 60
            )
            for index in range(hours)
        ),
    )


def quality(tmp_path, area="NL"):
    """Use generic source IDs so selection tests cannot depend on a particular provider."""
    return ForecastSelection(
        SqlAlchemySelectionStore.local(tmp_path), area, "primary", "alternative"
    )


def history(selection, *, error_a=0.02, error_b=0.015, bucket=1, area="NL", days=7):
    """Seed seven complete paired days. Zero actual price makes MAE independent arithmetic."""
    with closing(sqlite3.connect(selection.store.path)) as db, db:
        for index in range(days * 24):
            target = NOW - timedelta(days=days) + timedelta(hours=index)
            stamp = target.isoformat()
            db.execute(
                "INSERT INTO oep_price_pairs VALUES (?,?,?,?,?,?,?,?)",
                (
                    area,
                    bucket,
                    (target - timedelta(hours=30)).isoformat(),
                    stamp,
                    error_a,
                    error_b,
                    "a" * 64,
                    "b" * 64,
                ),
            )
            db.execute("INSERT INTO oep_price_actuals VALUES (?,?,?,?)", (area, stamp, 0, "c" * 64))


def assess(selection, day=0):
    """Install one completed atomic assessment exactly as the service owner does."""
    selection.states = selection.update((), NOW + timedelta(days=day))
    return selection.states[1]


def test_retired_provider_preference_does_not_select_or_describe_a_current_source(tmp_path):
    """A stored test-provider decision is ignored after normal adapters are installed. Its history remains intact."""
    from open_energy_planner.prices.selection import ForecastScore

    selection = quality(tmp_path)
    original = {"preferred_source": "retired-test-source", "streak": 99}
    selection.states[1] = original
    assert selection.preferred[1] == "primary"
    assert selection.state(1) == {}
    decision = selection.decide(original, ForecastScore(None, None, 0, 0), NOW)
    assert decision["preferred_source"] == "primary"
    assert decision["streak"] == 0
    assert selection.states[1] == original


def test_switch_requires_three_daily_assessments_and_survives_restart(tmp_path):
    """MAE 0.015 beats 0.020 by 25%. Only day three switches, and hourly checks/restart cannot accelerate it."""
    selection = quality(tmp_path)
    history(selection)
    assert assess(selection)["streak"] == 1
    assert assess(selection)["streak"] == 1
    selection = quality(tmp_path)
    assert selection.states[1]["streak"] == 1
    assert assess(selection, 1)["streak"] == 2
    assert assess(selection, 2)["preferred_source"] == "alternative"
    assert quality(tmp_path).preferred == ("primary", "alternative", "primary")


@pytest.mark.parametrize(
    "primary_error,alternative_error", [(0.02, 0.019), (0.004, 0.0035), (0, 0), (0.02, 0.02)]
)
def test_both_relative_and_absolute_improvement_are_required(
    tmp_path, primary_error, alternative_error
):
    """A sub-10% or sub-0.001 EUR/kWh improvement and ties never change preference."""
    selection = quality(tmp_path)
    history(selection, error_a=primary_error, error_b=alternative_error)
    for day in range(3):
        assert assess(selection, day)["preferred_source"] == "primary"
    assert selection.states[1]["reason"] == "retained_within_threshold"


def test_exact_threshold_and_missing_assessment_reset_streak(tmp_path):
    """Exactly 10% and 0.001 EUR/kWh qualifies. A skipped daily assessment restarts the required consecutive sequence."""
    selection = quality(tmp_path)
    history(selection, error_a=0.01, error_b=0.009)
    assert assess(selection)["streak"] == 1
    assert assess(selection, 2)["streak"] == 1
    assert assess(selection, 3)["streak"] == 2
    assert assess(selection, 4)["preferred_source"] == "alternative"


def test_area_bucket_and_thirty_day_window_are_isolated(tmp_path):
    """NL lead 24-48 evidence cannot change BE or other lead buckets. Expired evidence no longer supports a challenger."""
    selection = quality(tmp_path)
    history(selection)
    for day in range(3):
        assess(selection, day)
    assert quality(tmp_path, "BE").preferred == ("primary",) * 3
    assert selection.preferred == ("primary", "alternative", "primary")
    assert assess(selection, 40)["reason"] == "insufficient_evidence"
    assert selection.preferred[1] == "alternative"
    with closing(sqlite3.connect(selection.store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM oep_price_pairs").fetchone()[0] == 168


def test_insufficient_days_do_not_switch_and_bad_day_resets_streak(tmp_path):
    """Six complete days remain provisional. Losing the threshold resets a pending streak."""
    selection = quality(tmp_path)
    history(selection, days=6)
    assert assess(selection)["reason"] == "insufficient_evidence"
    with closing(sqlite3.connect(selection.store.path)) as db, db:
        db.execute("UPDATE oep_price_pairs SET alternative_price=0.03")
    assert assess(selection, 1)["streak"] == 0


def test_receipts_are_paired_once_without_hindsight_or_partial_hours(tmp_path):
    """Only common complete unknown hours from both current receipts are paired, with immutable source references."""
    selection = quality(tmp_path)
    primary_series, alternative_series = (
        series("primary", hours=48),
        series("alternative", value=-0.01, hours=72),
    )
    known = series("market", hours=24, kind=PriceKind.PUBLISHED)
    selection.update(
        (known, primary_series, alternative_series), NOW, (primary_series, alternative_series)
    )
    selection.update(
        (known, primary_series, alternative_series),
        NOW + timedelta(minutes=10),
        (primary_series, alternative_series),
    )
    with closing(sqlite3.connect(selection.store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM oep_price_pairs").fetchone()[0] == 24
        assert db.execute("SELECT DISTINCT bucket FROM oep_price_pairs").fetchall() == [(1,)]
        assert db.execute(
            "SELECT primary_version,alternative_version FROM oep_price_pairs LIMIT 1"
        ).fetchone() == (primary_series.version, alternative_series.version)
    assert (
        hourly(
            (
                replace(
                    primary_series,
                    points=(replace(primary_series.points[0], end=NOW + timedelta(minutes=30)),),
                ),
            )
        )
        == {}
    )
    selection.update(
        (primary_series, alternative_series),
        NOW,
        (
            primary_series,
            replace(alternative_series, received_at=NOW - timedelta(hours=1), issued_at=None),
        ),
    )
    with closing(sqlite3.connect(selection.store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM oep_price_pairs").fetchone()[0] == 24


def test_hourly_error_is_absolute_before_aggregation(tmp_path):
    """Opposite signed errors must not cancel. Repeated cohort samples give each delivery hour equal final weight."""
    selection = quality(tmp_path)
    history(selection, error_a=0.02, error_b=-0.01)
    state = assess(selection)
    assert state["primary_mae"] == pytest.approx(0.02)
    assert state["alternative_mae"] == pytest.approx(0.01)
    assert state["matched_hours"] == 168


def test_partial_published_hour_and_oversized_intervals_are_not_comparison_samples(tmp_path):
    """Even one known quarter excludes its delivery hour. Long malformed upstream spans cannot expand into huge hourly loops."""
    selection = quality(tmp_path)
    primary_series, alternative_series = series("primary"), series("alternative")
    actual = series("market", hours=1, kind=PriceKind.PUBLISHED)
    actual = replace(actual, points=(replace(actual.points[0], end=NOW + timedelta(minutes=15)),))
    selection.update(
        (actual, primary_series, alternative_series), NOW, (primary_series, alternative_series)
    )
    with closing(sqlite3.connect(selection.store.path)) as db:
        assert not db.execute(
            "SELECT 1 FROM oep_price_pairs WHERE target=?", (NOW.isoformat(),)
        ).fetchone()
    assert (
        hourly(
            (
                replace(
                    primary_series,
                    points=(replace(primary_series.points[0], end=NOW + timedelta(days=1000)),),
                ),
            )
        )
        == {}
    )


def test_failed_comparison_keeps_the_in_memory_preference_and_reports_issue(tmp_path):
    """After selection storage fails, a usable alternative still supplies prices without falsely reporting a successful assessment."""

    async def scenario():
        """Use the real service error boundary and preserve the deliberately corrupted bytes."""
        selection = quality(tmp_path)
        history(selection)
        for day in range(3):
            assess(selection, day)
        service = PriceService(
            "NL", None, None, make_store(tmp_path, PriceStore), selection=selection
        )
        service.series = (series("primary"), series("alternative"))
        selection.store.path.write_bytes(b"damaged fixture")
        await service.refresh(NOW)
        assert selection.preferred[1] == "alternative"
        assert (
            service.document(NOW)["issues"]["selection"] == "forecast_selection_storage_unavailable"
        )
        assert service.timeline(NOW).entries[96].source_id == "alternative"

    asyncio.run(scenario())


def test_hourly_refresh_collects_both_sources_and_does_not_repeat_requests(tmp_path):
    """The generic service records only same-round pairs, refreshes both hourly and keeps the surviving source on failure."""

    async def scenario():
        """Drive the production source coordinator with a fixed clock and typed offline responses."""

        class Source:
            """Track fetches while optionally simulating an unavailable provider."""

            def __init__(self, name):
                """Retain the opaque source identity and request counter."""
                self.name, self.calls, self.fail = name, 0, False

            async def fetch(self, now):
                """Return a current paired forecast or a sanitized availability error."""
                self.calls += 1
                if self.fail:
                    raise PricesUnavailable("fixture_outage")
                return series(self.name, start=now, receipt=now)

        primary_source, alternative_source = Source("primary"), Source("alternative")
        selector = quality(tmp_path)
        service = PriceService(
            "NL",
            None,
            primary_source,
            make_store(tmp_path, PriceStore),
            alternative=alternative_source,
            selection=selector,
        )
        await service.refresh(NOW)
        await service.refresh(NOW + timedelta(minutes=30))
        assert (primary_source.calls, alternative_source.calls) == (1, 1)
        with closing(sqlite3.connect(selector.store.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM oep_price_pairs").fetchone()[0] == 72
        alternative_source.fail = True
        await service.refresh(NOW + timedelta(hours=1))
        assert (primary_source.calls, alternative_source.calls) == (2, 2)
        assert service.issues["alternative_forecast"] == "fixture_outage"
        assert service.timeline(NOW + timedelta(hours=1)).status == "ready"

    asyncio.run(scenario())


def test_preference_never_replaces_published_prices_and_fallback_keeps_provenance():
    """Thirty published hours lead even across the 24-hour boundary. Preferred forecasts fill only unknown intervals."""
    primary_series, alternative_series = series("primary"), series("alternative", value=0.03)
    actual = series("market", hours=30, kind=PriceKind.PUBLISHED)
    timeline = combine(
        (primary_series, alternative_series, actual),
        NOW,
        market_area="NL",
        hours=72,
        preferred=("alternative", "primary", "alternative"),
    )
    assert all(
        price_entry.source_id == "market"
        for price_entry in timeline.entries
        if price_entry.start < NOW + timedelta(hours=30)
    )
    assert timeline.entries[120].source_id == "primary"
    assert timeline.entries[192].source_id == "alternative"
    assert timeline.entries[192].source_version == alternative_series.version
    fallback = combine(
        (primary_series, actual), NOW, market_area="NL", preferred=("alternative",) * 3
    )
    assert fallback.entries[-1].source_id == "primary"


class Client:
    """Capture fixed provider parameters and return a public-shape response without network requests."""

    def __init__(self, body):
        """Keep response mutation local to each adapter scenario."""
        self.body = body
        self.calls = []

    async def get(self, url, params):
        """Record the source boundary. No retail markup or API key is allowed."""
        self.calls.append((url, params))
        return self.body


def response():
    """Include known quarters, negative future prices and the API's inclusive extra hour."""
    return {
        "knownUntil": (NOW + timedelta(hours=29, minutes=45)).isoformat(),
        "prices": [
            {"startsAt": (NOW + timedelta(hours=index)).isoformat(), "total": -0.05}
            for index in range(73)
        ],
    }


def test_epex_public_normalization_clips_known_prices_and_inclusive_end():
    """Known 29:45 includes hour 29. Keep only hours 30-71 with original negative EUR/kWh values and unknown issue age."""
    client = Client(response())
    value = asyncio.run(EpexPredictor(client, "NL").fetch(NOW))
    assert len(value.points) == 42
    assert value.points[0].start == NOW + timedelta(hours=30)
    assert value.points[-1].end == NOW + timedelta(hours=72)
    assert all(
        point.value == -0.05 and point.native_resolution_minutes == 60 for point in value.points
    )
    assert value.issued_at is None
    assert value.expires_at == NOW + timedelta(hours=2)
    assert client.calls[0][1] == {
        "region": "NL",
        "hours": "72",
        "unit": "EUR_PER_KWH",
        "hourly": "true",
        "timezone": "UTC",
        "surcharge": "0",
        "taxPercent": "0",
    }


@pytest.mark.parametrize("bad", ["nan", "naive", "duplicate", "missing", "bool"])
def test_epex_rejects_malformed_provider_values(bad):
    """Malformed/ambiguous upstream rows are rejected, never normalized into free energy."""
    body = response()
    if bad == "nan":
        body["prices"][40]["total"] = float("nan")
    elif bad == "naive":
        body["prices"][40]["startsAt"] = "2026-10-23T16:00:00"
    elif bad == "duplicate":
        body["prices"][40] = body["prices"][39]
    elif bad == "bool":
        body["prices"][40]["total"] = True
    else:
        del body["knownUntil"]
    with pytest.raises(PricesUnavailable):
        asyncio.run(EpexPredictor(Client(body), "NL").fetch(NOW))


def test_epex_region_mapping_and_unsupported_market():
    """DE-LU maps only inside the adapter. Unsupported zones make no provider request."""
    client = Client(response())
    assert asyncio.run(EpexPredictor(client, "DE-LU").fetch(NOW)).market_area == "DE-LU"
    assert client.calls[0][1]["region"] == "DE"
    with pytest.raises(PricesUnavailable, match="unsupported"):
        asyncio.run(EpexPredictor(client, "IT-NORD").fetch(NOW))
    assert len(client.calls) == 1


def test_service_uses_preference_and_exposes_actual_fallback_and_errors(tmp_path):
    """Normalized public documents distinguish preference from used prices and preserve old evidence after an outage."""
    selection = quality(tmp_path)
    history(selection)
    for day in range(3):
        assess(selection, day)
    service = PriceService("NL", None, None, make_store(tmp_path, PriceStore), selection=selection)
    service.series = (series("primary"), series("alternative", value=0.03))
    before = service.document(NOW)
    assert before["forecast_selection"][1]["active_sources"] == ["alternative"]
    assert before["entries"][96]["source_id"] == "alternative"
    service.series = (service.series[0], replace(service.series[1], expires_at=NOW))
    after = service.document(NOW)
    assert after["forecast_selection"][1]["active_sources"] == ["primary"]
    assert after["forecast_selection"][1]["usage_reason"] == "available_source_fallback"
    assert before["entries"][96]["source_id"] == "alternative"


def test_corrupt_selection_storage_retains_known_preference_and_evidence(tmp_path):
    """A storage failure neither deletes the database nor invents a successful assessment."""
    selection = quality(tmp_path)
    selection.store.path.write_bytes(b"corrupt fixture")
    with pytest.raises(OSError, match="storage_unavailable"):
        selection.update((), NOW)
    assert selection.store.path.read_bytes() == b"corrupt fixture"
    restored = quality(tmp_path)
    assert restored.issue == "forecast_selection_storage_unavailable"


def test_failed_forecast_retries_before_cached_epex_expiry_without_refetching_healthy_source(
    tmp_path,
):
    """Hourly Epex failure retries at 90 minutes before its two-hour expiry. The healthy source stays on its hourly cadence."""

    async def scenario():
        """Drive independent source clocks with an explicit two-hour response validity bound."""

        class Source:
            """Expose current forecasts and one controlled temporary outage."""

            def __init__(self, name):
                """Keep independent calls and the simulated source state."""
                self.name, self.calls, self.fail = name, 0, False

            async def fetch(self, now):
                """Return typed usable coverage or an expected provider failure."""
                self.calls += 1
                if self.fail:
                    raise PricesUnavailable("fixture_outage")
                return replace(
                    series(self.name, start=now, receipt=now), expires_at=now + timedelta(hours=2)
                )

        primary_source, alternative_source = Source("primary"), Source("alternative")
        service = PriceService(
            "NL",
            None,
            primary_source,
            make_store(tmp_path, PriceStore),
            alternative=alternative_source,
        )
        await service.refresh(NOW)
        alternative_source.fail = True
        await service.refresh(NOW + timedelta(hours=1))
        await service.refresh(NOW + timedelta(hours=1, minutes=29))
        assert (primary_source.calls, alternative_source.calls) == (2, 2)
        assert (
            service.timeline(NOW + timedelta(hours=1, minutes=29), hours=72).status != "unavailable"
        )
        alternative_source.fail = False
        await service.refresh(NOW + timedelta(hours=1, minutes=30))
        assert (primary_source.calls, alternative_source.calls) == (2, 3)
        assert "alternative_forecast" not in service.issues
        assert next(
            series_item for series_item in service.series if series_item.source_id == "alternative"
        ).expires_at == NOW + timedelta(hours=3, minutes=30)

    asyncio.run(scenario())
