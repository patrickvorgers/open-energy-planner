"""Independent historical export-budget arithmetic with real immutable frame storage."""

from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.contracts.bonus_statistics import StatisticReference, StatisticsBasis
from open_energy_planner.contracts.export_meter import export_identity, seed_reading
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.savings.accounting import bonus_payment
from open_energy_planner.savings.bonus import HistoricalBonuses
from open_energy_planner.savings.model import Frame, PendingSettlement
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import archive_engine
from tests.app.savings.support.scenarios import START, contract

EXPORTS = ("sensor.export_a", "sensor.export_b")


def measured_rows(*, failure=None):
    """Supply .50, .50 and 1.00 kWh quarters. At minute 35 exactly 1.15 kWh has been exported."""
    meters = [(100, 200), (100.2, 200.3), (100.6, 200.4), (101.2, 200.8)]
    if failure in ("early_reset", "boundary_reset"):
        meters = [(100, 200), (1, 200.3), (1.4, 200.4), (2, 200.8)]
    if failure == "anchor_reset":
        meters[-1] = (1.2, 200.8)
    rows = []
    for index, (values, deltas) in enumerate(
        zip(meters, [(0.2, 0.3), (0.4, 0.1), (0.6, 0.4), (0, 0)])
    ):
        if failure == "gap" and index == 1 or failure == "missing_end" and index == 3:
            continue
        start = START + timedelta(minutes=15 * index)
        readings = dict(zip(EXPORTS, values))
        if failure == "missing_member" and index == 1:
            del readings[EXPORTS[1]]
        frame = Frame(
            start,
            start + timedelta(minutes=15),
            dict(zip(EXPORTS, deltas)),
            {},
            {},
            readings,
            "recorded",
        )
        sources = (
            list(EXPORTS)
            if failure != "changed_source" or index != 1
            else [EXPORTS[0], "sensor.other"]
        )
        rows.append((frame, {"bindings": {"exports": sources}}))
    return rows


def anchor(*, failure=None):
    """Confirm 10 kWh used at minute 35 with .10+.05 kWh after the minute-30 readings."""
    components = ((EXPORTS[0], 1.5 if failure == "early_reset" else 100.7), (EXPORTS[1], 200.45))
    if failure == "bad_anchor":
        components = ((EXPORTS[0], 100.5), (EXPORTS[1], 201))
    if failure == "boundary_reset":
        components = ((EXPORTS[0], 1), (EXPORTS[1], 200.3))
    return CounterSeed(
        "bonus",
        contract(bonus=True).bonuses[0].period_start(START),
        0.5 if failure == "negative_usage" else 10,
        sum(value for _, value in components),
        export_identity(EXPORTS),
        START + timedelta(minutes=15 if failure == "boundary_reset" else 35),
        components,
    )


@pytest.fixture
def evidence(tmp_path):
    """Use a disposable app-owned SQLite archive. Backend-wide persistence has separate matrix coverage."""
    engine = archive_engine(tmp_path)
    SCHEMA.create_all(engine)
    store = SettlementStore(engine)
    yield store, HistoricalBonuses(store)
    engine.dispose()


def retain(store, rows):
    """Archive separately bound immutable measurement frames without changing their independent values."""
    for frame, context in rows:
        store.put_frames((frame,), context)


def test_multiple_export_meters_initialize_one_budget_and_cannot_mask_resets():
    """Used 1+.2+.1 leaves .7 kWh at a 2-kWh cap, paying .07 on 1 kWh exported."""
    seed = replace(
        anchor(),
        used_kwh=1,
        observed_at=START,
        meter_kwh=300,
        component_meters=((EXPORTS[0], 100), (EXPORTS[1], 200)),
    )
    counters = {}
    paid = bonus_payment(
        contract(bonus=True),
        START,
        START + timedelta(minutes=15),
        0.1,
        1,
        counters,
        (seed,),
        {EXPORTS[0]: 100.2, EXPORTS[1]: 200.1, "sensor.import": 99999},
        None,
    )
    assert paid == pytest.approx(0.07)
    assert list(counters.values()) == pytest.approx([2.3])
    with pytest.raises(ValueError, match="reset"):
        seed_reading(seed, {EXPORTS[0]: 99, EXPORTS[1]: 202})
    with pytest.raises(ValueError, match="membership"):
        seed_reading(seed, {EXPORTS[0]: 100, "sensor.unrelated": 200})


@pytest.mark.parametrize(
    "scenario,expected", [(None, 8.85), ("early_reset", 8.85), ("boundary_reset", 9.5)]
)
def test_later_confirmed_usage_is_backdated_using_measured_energy(evidence, scenario, expected):
    """Subtract .50+.50+.15 from 10. Earlier complete-frame resets preserve their Recorder deltas."""
    store, baselines = evidence
    retain(store, measured_rows(failure=scenario))
    original = anchor(failure=scenario)
    before = store.frames()
    earlier = baselines.backdate(original, START, EXPORTS)
    assert earlier.used_kwh == pytest.approx(expected)
    assert earlier.observed_at == START
    assert earlier.meter_kwh == 300
    assert earlier.component_meters == ((EXPORTS[0], 100), (EXPORTS[1], 200))
    assert original.used_kwh == 10
    assert store.frames() == before


@pytest.mark.parametrize(
    "failure,diagnostic",
    [
        ("gap", "gap"),
        ("missing_member", "gap"),
        ("changed_source", "changed meters"),
        ("anchor_reset", "reset inside"),
        ("missing_end", "awaits captured"),
        ("bad_anchor", "outside"),
        ("negative_usage", "exceed"),
    ],
)
def test_backdating_refuses_incomplete_or_conflicting_evidence(evidence, failure, diagnostic):
    """Unknown subinterval exports, changed members, resets at the anchor and negative used volume are never invented."""
    store, baselines = evidence
    retain(store, measured_rows(failure=failure))
    with pytest.raises(PendingSettlement, match=diagnostic):
        baselines.backdate(anchor(failure=failure), START, EXPORTS)


@pytest.mark.parametrize(
    "availability,observed_hours,backdate_cached",
    [
        ("valid", -1, False),
        ("valid", 0, False),
        *[
            (availability, observed_hours, backdate_cached)
            for availability in ("issue", "unknown_prefix", "no_physical")
            for observed_hours, backdate_cached in ((-1, False), (0, False), (1, False), (1, True))
        ],
    ],
)
def test_selected_statistics_seed_requires_complete_evidence(
    evidence, observed_hours, availability, backdate_cached
):
    """Invalid older, exact, direct and cached anchors wait. Valid historical endpoints remain usable."""
    _, baselines = evidence
    frame, context = measured_rows()[0]
    reference = START - timedelta(hours=2)
    observed = START + timedelta(hours=observed_hours)
    prefix_kwh = None if availability == "unknown_prefix" else 5
    physical_available = availability != "no_physical"
    basis = StatisticsBasis(
        reference,
        observed,
        tuple(
            StatisticReference(entity, index + 1, "recorder", "kWh", 0, 1)
            for index, entity in enumerate(EXPORTS)
        ),
        prefix_kwh,
        reference if prefix_kwh is not None else None,
        "a" * 64,
        physical_available,
        issue="bonus_statistics_revalidation_required" if availability == "issue" else None,
    )
    seed = CounterSeed(
        "bonus",
        contract(bonus=True).bonuses[0].period_start(START),
        None if prefix_kwh is None else 7,
        300 if physical_available else None,
        export_identity(EXPORTS),
        observed,
        ((EXPORTS[0], 100), (EXPORTS[1], 200)) if physical_available else (),
        basis,
    )
    if backdate_cached:
        baselines.backdated[(seed, frame.start, tuple(sorted(EXPORTS)))] = seed

    def use_anchor():
        """Exercise normal selection or direct backdating without bypassing either evidence guard."""
        if observed_hours > 0:
            return baselines.backdate(seed, frame.start, EXPORTS)
        return baselines.prepare(
            contract(bonus=True),
            frame,
            context["bindings"],
            (seed,),
            None,
            START + timedelta(hours=3),
            "historical-bindings",
        )

    if availability == "valid":
        assert use_anchor() == (seed,)
    else:
        with pytest.raises(PendingSettlement, match="Validated physical bonus endpoint"):
            use_anchor()


@pytest.mark.parametrize("blocked", [False, True])
def test_current_confirmation_is_not_replaced_by_older_archive(evidence, blocked):
    """A corrected 5-kWh opening exceeds the 2-kWh cap, so payment is zero.

    Revalidation must not restore the old 1-kWh opening and its remaining allowance.
    """
    store, baselines = evidence
    rows = measured_rows()
    endpoint = START + timedelta(hours=1)
    tail = replace(
        rows[-1][0],
        start=endpoint,
        end=endpoint + timedelta(minutes=15),
        channels={EXPORTS[0]: 0, EXPORTS[1]: 0},
        meter_start={EXPORTS[0]: 101.2, EXPORTS[1]: 200.8},
    )
    retain(store, [*rows, (tail, rows[-1][1])])
    original = replace(
        anchor(),
        used_kwh=1,
        observed_at=START,
        meter_kwh=300,
        component_meters=((EXPORTS[0], 100), (EXPORTS[1], 200)),
    )
    archived = CounterBook(START, (original,))
    baselines.archive.save(archived)
    basis = StatisticsBasis(
        START,
        endpoint,
        (
            StatisticReference(EXPORTS[0], 1, "recorder", "kWh", 0, 1.2),
            StatisticReference(EXPORTS[1], 2, "recorder", "kWh", 0, 0.8),
        ),
        5,
        START,
        "a" * 64,
        True,
        issue="bonus_statistics_revalidation_required" if blocked else None,
    )
    current = replace(
        original,
        used_kwh=7,
        meter_kwh=302,
        observed_at=endpoint,
        component_meters=((EXPORTS[0], 101.2), (EXPORTS[1], 200.8)),
        statistics=basis,
    )

    def prepare():
        """Select the same-period correction against the existing usable historical archive."""
        return baselines.prepare(
            contract(bonus=True),
            rows[0][0],
            rows[0][1]["bindings"],
            (),
            CounterBook(endpoint, (current,)),
            endpoint + timedelta(hours=1),
            "bindings",
        )

    if blocked:
        with pytest.raises(PendingSettlement, match="Validated physical bonus endpoint"):
            prepare()
        assert baselines.pending is None
    else:
        seeds = prepare()
        assert seeds[0].used_kwh == pytest.approx(5)
        assert (
            bonus_payment(
                contract(bonus=True),
                START,
                START + timedelta(minutes=15),
                0.1,
                1,
                {},
                seeds,
                rows[0][0].meter_start,
                None,
            )
            == 0
        )
    assert baselines.archive.current == archived
    assert original.used_kwh == 1 and current.used_kwh == 7


def test_unseen_historical_period_requests_complete_reset_aware_ha_history(evidence):
    """A missing old period is derived from full hourly sums, with per-meter final readings and no planning confirmation changes."""
    store, baselines = evidence
    rows = measured_rows()
    retain(store, rows)
    with pytest.raises(PendingSettlement, match="being read"):
        baselines.prepare(
            contract(bonus=True),
            rows[0][0],
            rows[0][1]["bindings"],
            (),
            None,
            START + timedelta(hours=3),
            "old-bindings",
        )
    request = baselines.pending
    period = request.periods[0][1]
    assert request.entity_ids == EXPORTS
    assert request.end == START
    assert request.document()["start"] == (period - timedelta(hours=1)).isoformat()
    count = int((START - period).total_seconds() / 3600) + 1
    hours = tuple(
        EnergyHour(period + timedelta(hours=index - 1), 100 + index * 0.5, 300)
        for index in range(count)
    )
    history = EnergyHistory(export_identity(EXPORTS), hours, ((EXPORTS[0], 100), (EXPORTS[1], 200)))
    with pytest.raises(ValueError, match="Incomplete"):
        baselines.receive(
            request, replace(history, hours=hours[:2] + hours[3:]), START + timedelta(hours=3)
        )
    assert baselines.archive.current is None
    baselines.receive(request, history, START + timedelta(hours=3))
    restarted = HistoricalBonuses(store)
    seeds = restarted.prepare(
        contract(bonus=True),
        rows[0][0],
        rows[0][1]["bindings"],
        (),
        None,
        START + timedelta(hours=3),
        "old-bindings",
    )
    assert len(seeds) == 1
    assert seeds[0].used_kwh == (count - 1) * 0.5
    assert seeds[0].observed_at == START
    assert restarted.pending is None


def test_single_export_meter_backdating_crosses_bounded_storage_pages(evidence):
    """One hundred .25-kWh measured quarters subtract exactly 25 kWh from a later 30-kWh confirmation."""
    store, baselines = evidence
    for index in range(101):
        start = START + timedelta(minutes=15 * index)
        frame = Frame(
            start,
            start + timedelta(minutes=15),
            {"sensor.export": 0.25},
            {},
            {},
            {"sensor.export": 100 + 0.25 * index},
            "recorded",
        )
        store.put_frames((frame,), {"bindings": {"exports": ["sensor.export"]}})
    seed = CounterSeed(
        "bonus",
        contract(bonus=True).bonuses[0].period_start(START),
        30,
        125,
        "sensor.export",
        START + timedelta(hours=25),
    )
    earlier = baselines.backdate(seed, START, ("sensor.export",))
    assert earlier.used_kwh == 5
    assert earlier.meter_kwh == 100


@pytest.mark.parametrize("timezone", ["Asia/Kolkata", "Asia/Kathmandu"])
def test_non_hour_reset_never_requests_unprovable_hourly_history(evidence, timezone):
    """A half-hour or quarter-hour UTC reset cannot be recovered exactly from hourly statistics."""
    _store, baselines = evidence
    selected = contract(bonus=True)
    selected = replace(selected, bonuses=(replace(selected.bonuses[0], timezone=timezone),))
    with pytest.raises(PendingSettlement, match="whole UTC hour"):
        baselines.prepare(
            selected,
            measured_rows()[0][0],
            {"exports": EXPORTS},
            (),
            None,
            START + timedelta(hours=3),
            "historical-bindings",
        )
    assert baselines.pending is None


@pytest.mark.parametrize("cached_count", [2, 64])
def test_historical_reception_retains_old_periods_and_always_keeps_requested_seed(
    evidence, cached_count
):
    """A new older request survives a full cache. Other periods remain reusable after restart within the 64-seed limit."""
    from open_energy_planner.contracts.bonus import CounterBook
    from open_energy_planner.contracts.bonus_history import BaselineRequest

    store, baselines = evidence
    seeds = tuple(replace(anchor(), rule_id=f"retained-{index}") for index in range(cached_count))
    baselines.archive.save(CounterBook(START + timedelta(hours=1), seeds))
    period = START - timedelta(hours=1)
    request = BaselineRequest(
        "contract",
        "bindings",
        None,
        export_identity(EXPORTS),
        (("older-rule", period),),
        (("older-rule", period),),
        START,
        EXPORTS,
    )
    history = EnergyHistory(
        export_identity(EXPORTS),
        (
            EnergyHour(START - timedelta(hours=2), 100, 300),
            EnergyHour(START - timedelta(hours=1), 101, 300),
        ),
        ((EXPORTS[0], 100), (EXPORTS[1], 200)),
    )
    baselines.receive(request, history, START + timedelta(hours=2))
    restarted = HistoricalBonuses(store)
    kept = restarted.archive.current.seeds
    assert len(kept) == min(64, cached_count + 1)
    older = next(seed for seed in kept if seed.rule_id == "older-rule")
    assert older.used_kwh == 1
    assert older.observed_at == START
    assert sum(seed in kept for seed in seeds) == min(63, cached_count)


@pytest.mark.parametrize("minute", [0, 15])
@pytest.mark.parametrize("prefix", [0, 5])
@pytest.mark.parametrize(
    "fault", [None, "metadata", "reference", "physical", "missing_end", "stale_book"]
)
def test_statistics_replay_requests_historical_endpoint_without_latest_capture(
    evidence, minute, prefix, fault
):
    """A June contract replays November from annual sums without capturing quarters up to today's moving counter."""
    from datetime import UTC, datetime

    store, baselines = evidence
    period = datetime(2026, 6, 1, tzinfo=UTC)
    start = datetime(2026, 11, 1, 13, minute, tzinfo=UTC)
    endpoint = start.replace(minute=0) + timedelta(hours=bool(minute))
    latest = start.replace(minute=0) + timedelta(days=5)
    reference = StatisticsBasis(
        period + timedelta(days=bool(prefix)),
        latest,
        (
            StatisticReference(EXPORTS[0], 9, "recorder", "kWh", 1000, 1100),
            StatisticReference(EXPORTS[1], 10, "recorder", "kWh", 2000, 2050),
        ),
        prefix,
        period + timedelta(days=1) if prefix else None,
        "a" * 64,
        True,
    )
    current = CounterSeed(
        "bonus",
        period,
        150 + prefix,
        800,
        export_identity(EXPORTS),
        latest,
        ((EXPORTS[0], 400), (EXPORTS[1], 400)),
        reference,
    )
    selected = contract(bonus=True)
    selected = replace(selected, bonuses=(replace(selected.bonuses[0], contract_start=period),))
    rows = []
    for index in range(5):
        instant = start + timedelta(minutes=15 * index)
        frame = Frame(
            instant,
            instant + timedelta(minutes=15),
            dict.fromkeys(EXPORTS, 0),
            {},
            {},
            {EXPORTS[0]: 300, EXPORTS[1]: 400},
            "recorded",
        )
        rows.append((frame, {"bindings": {"exports": EXPORTS}}))
    retain(store, rows)
    book = CounterBook(latest, (current,))
    with pytest.raises(PendingSettlement, match="being read from Home Assistant"):
        baselines.prepare(
            selected,
            rows[0][0],
            rows[0][1]["bindings"],
            (),
            book,
            latest + timedelta(hours=3),
            "bindings",
        )
    request = baselines.pending
    assert request.statistics_sum and request.end == endpoint
    assert request.counter_version == book.version

    from open_energy_planner.contracts.bonus_statistics import StatisticsChannel

    channels = tuple(
        StatisticsChannel(
            identity,
            (
                (reference.reference_at - timedelta(hours=1), identity.reference_sum, 0),
                (endpoint - timedelta(hours=1), total, physical),
            ),
        )
        for identity, total, physical in zip(reference.series, (1080, 2020), (300, 400))
    )
    original_frames = store.frames()
    if fault:
        first = channels[0]
        if fault == "metadata":
            channels = (
                replace(first, identity=replace(first.identity, metadata_id=99)),
                *channels[1:],
            )
        elif fault == "reference":
            channels = (
                replace(first, hours=((first.hours[0][0], 999, 0), first.hours[1])),
                *channels[1:],
            )
        elif fault == "physical":
            channels = (
                replace(
                    first, hours=(first.hours[0], (first.hours[1][0], first.hours[1][1], None))
                ),
                *channels[1:],
            )
        elif fault == "missing_end":
            channels = (replace(first, hours=first.hours[:1]), *channels[1:])
        else:
            book = replace(book, recorded_at=latest + timedelta(hours=1))
        with pytest.raises(ValueError):
            baselines.receive_statistics(request, channels, book, latest + timedelta(hours=3))
        assert baselines.archive.current is None and store.frames() == original_frames
        return
    baselines.receive_statistics(request, channels, book, latest + timedelta(hours=3))
    seeds = baselines.prepare(
        selected,
        rows[0][0],
        rows[0][1]["bindings"],
        (),
        book,
        latest + timedelta(hours=3),
        "bindings",
    )
    assert seeds[0].used_kwh == 100 + prefix
    assert seeds[0].observed_at == start
    assert seeds[0].component_meters == ((EXPORTS[0], 300), (EXPORTS[1], 400))
    assert current.used_kwh == 150 + prefix and book.seeds == (current,)
    assert store.frames() == original_frames
    assert (
        baselines.archive.provenance(baselines.archive.current.seeds[0])["statistics"]["prefix_kwh"]
        == prefix
    )
    restarted = HistoricalBonuses(store)
    with pytest.raises(PendingSettlement, match="being read from Home Assistant"):
        restarted.prepare(
            selected,
            rows[0][0],
            rows[0][1]["bindings"],
            (),
            book,
            latest + timedelta(hours=3),
            "bindings",
        )
    # A revalidated historical correction must not silently reuse an older endpoint.
    updated = replace(book, recorded_at=latest + timedelta(hours=1))
    with pytest.raises(PendingSettlement, match="being read from Home Assistant"):
        baselines.prepare(
            selected,
            rows[0][0],
            rows[0][1]["bindings"],
            (),
            updated,
            latest + timedelta(hours=3),
            "bindings",
        )


@pytest.mark.parametrize("multiple", [False, True])
def test_statistics_period_start_checkpoint_is_reused_after_historical_ingress(evidence, multiple):
    """At contract start the boundary row proves zero export. A returned first-hour checkpoint must not cause endless retries."""
    from open_energy_planner.contracts.bonus_statistics import StatisticsChannel

    store, baselines = evidence
    exports = EXPORTS if multiple else EXPORTS[:1]
    count = len(exports)
    selected = contract(bonus=True)
    selected = replace(
        selected,
        bonuses=(replace(selected.bonuses[0], effective_from=START, contract_start=START),),
    )
    now = START + timedelta(days=1)
    basis = StatisticsBasis(
        START,
        now,
        tuple(
            StatisticReference(entity, index + 1, "recorder", "kWh", 1000, 1000 + 10 / count)
            for index, entity in enumerate(exports)
        ),
        0,
        None,
        "a" * 64,
        True,
    )
    current = CounterSeed(
        "bonus",
        START,
        10,
        310,
        export_identity(exports),
        now,
        tuple((entity, 310 / count) for entity in exports),
        basis,
    )
    book = CounterBook(now, (current,))
    # The first hour exports .5+.5+1+1=3 kWh, but today's 10-kWh endpoint is not captured.
    quarters = ((300, 0.5), (300.5, 0.5), (301, 1), (302, 1), (303, 0))
    rows = tuple(
        (
            Frame(
                START + timedelta(minutes=15 * index),
                START + timedelta(minutes=15 * (index + 1)),
                dict.fromkeys(exports, exported / count),
                {},
                {},
                dict.fromkeys(exports, meter / count),
                "recorded",
            ),
            {"bindings": {"exports": exports}},
        )
        for index, (meter, exported) in enumerate(quarters)
    )
    retain(store, rows)
    original = store.frames()

    def prepare():
        """Retry the real historical selection with the exact same current counter book."""
        return baselines.prepare(
            selected, rows[0][0], rows[0][1]["bindings"], (), book, now, "period-start-bindings"
        )

    with pytest.raises(PendingSettlement, match="being read from Home Assistant"):
        prepare()
    pending = baselines.pending
    # Supply exactly the requested endpoint. The unfixed request asks for the first hour
    # rather than the already sufficient zero-difference period boundary.
    channels = tuple(
        StatisticsChannel(
            identity,
            ((START - timedelta(hours=1), 1000, 300 / count),)
            + (((START, 1000 + 3 / count, 303 / count),) if pending.end > START else ()),
        )
        for identity in basis.series
    )
    baselines.receive_statistics(pending, channels, book, now)
    recovered = prepare()
    assert pending.end == START
    assert recovered[0].observed_at == START
    assert recovered[0].used_kwh == 0
    assert recovered[0].component_meters == tuple((entity, 300 / count) for entity in exports)
    assert recovered[0].statistics.reference_version == basis.reference_version
    assert recovered[0].statistics.latest_at == START
    assert baselines.pending is None
    assert store.frames() == original and book.seeds == (current,)
