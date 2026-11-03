"""Settlement continuity, corrections and complete evidence on every advertised database backend."""

import json
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame
from tests.database.support.history_datasets import settlement_batches


def test_settlement_restart_idempotency_and_period_filtering(settlement):
    """Two quarters cost actual .60 versus reference .40 inventory. Selecting the second retains -.10 benefit."""
    runtime, _store, _docs, engine, plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    first = runtime.overview(START, END + timedelta(minutes=15))
    assert first["savings"] == pytest.approx(-0.2)
    json.dumps(first, allow_nan=False)  # MySQL integer SUM must not leak Decimal into the API.
    assert first["coverage"] == {"evaluated": 2, "expected": 2, "estimated": 0}
    restarted = SavingsRuntime(SettlementStore(engine), SqlDocuments(engine, "savings"), plans)
    restarted.process(contract(), END + timedelta(hours=2))
    second = restarted.overview(END, END + timedelta(minutes=15))
    assert second["savings"] == pytest.approx(-0.1)
    assert second["intervals"][0]["reference"]["stored"] == {"battery": 0}
    assert restarted.overview(START, END + timedelta(minutes=15)) == first


def test_reconstructed_soc_quality_survives_restart(settlement, tmp_path):
    """Estimated SoC evidence remains immutable and labelled after restart on all four backends."""

    runtime, store, docs, engine, _plans = settlement
    original = store.frames()
    reconstructed = replace(frame(start=END + timedelta(minutes=15)), quality="estimated_soc")
    context = {"bindings": bindings(), "seeds": [], "location": None}
    store.put_frames((reconstructed,), context)
    runtime.advance_reference()
    assert runtime.physical_issue is None
    assert docs.read("physical-reference.json")["reference_stored"] == {"battery": 0}
    restarted = SettlementStore(engine)
    assert restarted.frames()[:2] == original
    assert restarted.frames()[-1]["frame"]["quality"] == "estimated_soc"


def test_corrected_contract_creates_new_evaluation_preserving_original_plan(settlement):
    """Changing actual import .30 to .40 changes total benefit -.20 to -.40. Old evaluated evidence remains readable."""
    runtime, store, _docs, _engine, plans = settlement
    original = plans.get_plan("original-plan")
    runtime.process(contract(), END + timedelta(hours=1))
    before = runtime.overview(START, END + timedelta(minutes=15))
    runtime.process(contract(buy="0.4"), END + timedelta(hours=2))
    after = runtime.overview(START, END + timedelta(minutes=15))
    assert after["savings"] == pytest.approx(-0.4)
    assert before["revision"] != after["revision"]
    assert len(store.results(before["revision"], START, END + timedelta(minutes=15))) == 2
    assert plans.get_plan("original-plan") == original


def test_ingestion_retries_do_not_duplicate_or_replace_meter_evidence(settlement):
    """Identical evidence is idempotent. Conflicting meter totals never silently rewrite evaluated history."""
    _runtime, store, _docs, _engine, _plans = settlement
    context = {"bindings": bindings(), "seeds": [], "location": None}
    store.put_frames((frame(),), context)
    assert len(store.frames()) == 2
    with pytest.raises(ValueError, match="immutable"):
        store.put_frames((replace(frame(), channels={"sensor.import": 2}),), context)
    assert store.frames()[0]["frame"]["channels"]["sensor.import"] == 1


def test_new_published_correction_reprices_without_reoptimizing(settlement):
    """A changed published source starts a new assessment generation even when fixed retail formulas keep costs equal."""
    runtime, _store, _docs, engine, _plans = settlement
    now = END + timedelta(hours=1)
    runtime.process(contract(), now)
    first = runtime.overview(START, END + timedelta(minutes=15))
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "published",
                    "NL",
                    "EUR",
                    now + timedelta(minutes=1),
                    None,
                    None,
                    (PricePoint(START, END, 0.9, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    runtime.process(contract(), now + timedelta(minutes=2))
    corrected = runtime.overview(START, END + timedelta(minutes=15))
    assert first["revision"] != corrected["revision"]
    assert corrected["savings"] == first["savings"]
    assert corrected["intervals"][0]["actual"]["tariffs"][0]["market_price"] == 0.9


def test_background_plan_repricing_publishes_only_successful_versions(settlement):
    """Corrected .40 costs are separate. A later uncovered contract leaves that successful valuation and original plan intact."""
    from open_energy_planner.savings.repricing import PlanValuations
    from tests.app.savings.test_plan_repricing import original_plan

    _runtime, _store, docs, _engine, plans = settlement
    original = original_plan()
    plans.state["plan"] = original
    plans.save()
    values = PlanValuations(docs, plans)
    values.process(contract(buy="0.4"), None, END)
    first = values.latest(original["plan_id"])
    assert first["valuation"]["ems_costs"]["purchase"] == pytest.approx(0.4)
    broken = replace(contract(), periods=(replace(contract().periods[0], effective_from=END),))
    values.process(broken, None, END + timedelta(hours=1))
    after = values.latest(original["plan_id"])
    assert after["valuation"] == first["valuation"]
    assert after["valuation_status"]["issue"]
    assert plans.get_plan(original["plan_id"]) == original


def test_sparse_multi_year_query_preserves_year_bounds_and_bounded_details(settlement):
    """Ninety-seven quarters inside 2023 exclude both neighbors and retain the detail and calendar limits."""
    from datetime import UTC, datetime

    from open_energy_planner.savings.storage.adapters.sqlalchemy import (
        SETTLEMENT_EVALUATIONS,
        SETTLEMENT_FRAMES,
    )

    runtime, store, docs, engine, _plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    head = docs.read("head.json")
    sample = store.results(head["revision"], START, END)[0]
    windows = [
        (datetime(2022, 12, 31, 23, 45, tzinfo=UTC), 1),
        (datetime(2023, 1, 1, tzinfo=UTC), 97),
        (datetime(2024, 1, 1, tzinfo=UTC), 1),
    ]
    for first, quarters in windows:
        for frames, evaluations in settlement_batches(
            sample, head["revision"], first, first + timedelta(minutes=15 * quarters)
        ):
            with engine.begin() as connection:
                connection.execute(SETTLEMENT_FRAMES.insert(), frames)
                connection.execute(SETTLEMENT_EVALUATIONS.insert(), evaluations)
    result = runtime.overview(datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC))
    assert result["coverage"]["evaluated"] == 97
    assert result["savings"] == pytest.approx(-9.7)
    assert len(result["intervals"]) == 96
    assert result["details_truncated"]
    assert [row["start"] for row in result["chart"]] == ["2023-01-01", "2023-01-02"]
    full = runtime.overview(datetime(2022, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))
    assert full["coverage"]["evaluated"] == 99
    assert [row["start"] for row in full["chart"]] == ["2022-12-01", "2023-01-01", "2024-01-01"]


def test_correction_uses_ingestion_order_not_provider_receipt_time(settlement):
    """An HTTP refresh received before the last poll but committed after it still triggers retrospective correction."""
    runtime, _store, _docs, engine, _plans = settlement
    now = END + timedelta(hours=1)
    runtime.process(contract(), now)
    first = runtime.overview(START, END)
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "late-refresh",
                    "NL",
                    "EUR",
                    START,
                    None,
                    None,
                    (PricePoint(START, END, 0.8, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    runtime.process(contract(), now + timedelta(minutes=1))
    after = runtime.overview(START, END)
    assert first["revision"] != after["revision"]
    assert after["intervals"][0]["actual"]["tariffs"][0]["market_price"] == 0.8


def test_changed_price_cannot_commit_a_stale_interval(settlement):
    """A price corrected after evaluation was read rejects the stale commit without storing evaluation or continuation."""
    from sqlalchemy import func, select

    from open_energy_planner.savings.model import PendingSettlement
    from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_EVALUATIONS

    runtime, store, _docs, engine, _plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    first = runtime.overview(START, END)["intervals"][0]
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "race",
                    "NL",
                    "EUR",
                    START,
                    None,
                    None,
                    (PricePoint(START, END, 0.8, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    with pytest.raises(PendingSettlement, match="changed"):
        store.commit("stale-evaluation", first, {})
    assert store.state("stale-evaluation") is None
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(SETTLEMENT_EVALUATIONS)
                .where(SETTLEMENT_EVALUATIONS.c.revision == "stale-evaluation")
            )
            == 0
        )


def test_future_reference_continues_recorded_scenario_state(settlement):
    """Reference depleted its original two kWh while actual remains full. The next forecast cannot borrow actual energy."""
    from open_energy_planner.savings.forecast_state import initial_state

    runtime, store, docs, _engine, plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    next_start = END + timedelta(minutes=15)
    document = plans.get_plan("original-plan")
    document["input_snapshot"]["problem"]["intervals"][0]["start"] = next_start.isoformat()
    state = initial_state(store, docs, plans, document, contract())
    assert state["stored"] == {"battery": 0}
    assert state["status"] == "continuous_reference"
    assert not state["estimated"]
    document["input_snapshot"]["problem"]["intervals"][0]["start"] = (
        next_start + timedelta(hours=2)
    ).isoformat()
    from open_energy_planner.savings.model import PendingSettlement

    with pytest.raises(PendingSettlement, match="recent metering"):
        initial_state(store, docs, plans, document, contract())


def test_failed_settlement_correction_preserves_latest_successful_view(settlement):
    """An uncovered corrected contract cannot replace the two settled quarters with an empty default evaluation."""
    runtime, _store, _docs, _engine, _plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    before = runtime.overview(START, END + timedelta(minutes=15))
    broken = replace(contract(), periods=(replace(contract().periods[0], effective_from=END),))
    runtime.process(broken, END + timedelta(hours=2))
    after = runtime.overview(START, END + timedelta(minutes=15))
    assert after["revision"] == before["revision"]
    assert after["totals"] == before["totals"]
    assert after["coverage"] == before["coverage"]
    assert after["issue"] == "Contract coverage is missing"


def test_partial_correction_publishes_after_existing_coverage_is_replayed(settlement, monkeypatch):
    """One-quarter batches retain the old two-quarter view until correction has caught up, then publish both corrected quarters."""
    runtime, store, docs, _engine, _plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    before = runtime.overview(START, END + timedelta(minutes=15))
    frames = store.frames
    monkeypatch.setattr(store, "frames", lambda after=None: frames(after, limit=1))
    runtime.process(contract(buy="0.4"), END + timedelta(hours=2))
    assert runtime.overview(START, END)["revision"] == before["revision"]
    assert docs.read("working.json") is not None
    runtime.process(contract(buy="0.4"), END + timedelta(hours=3))
    after = runtime.overview(START, END + timedelta(minutes=15))
    assert after["revision"] != before["revision"]
    assert after["coverage"]["evaluated"] == 2
    assert after["savings"] == pytest.approx(-0.4)
    assert docs.read("working.json") is None


def test_contract_correction_revalues_settlement_inventory_with_frozen_wholesale(settlement):
    """Corrected .40 usable-volume-capped value leaves the reference's first kWh above its one-kWh value cap. Benefit is -.40."""
    from open_energy_planner.contracts.model import TariffEvidence

    runtime, _store, _docs, engine, plans = settlement
    document = plans.get_plan("original-plan")
    document["plan_id"] = "valued-plan"
    document["input_snapshot"]["contract_version"] = contract().version
    rate = TariffEvidence(
        START,
        END,
        0.1,
        0.3,
        -0.1,
        "published",
        15,
        "test-market",
        "original-price",
        contract().version,
        0,
    )
    document["input_snapshot"]["tariff_evidence"] = json.loads(
        json.dumps([asdict(rate)], default=lambda value: value.isoformat())
    )
    plans.state["plan"] = document
    plans.save()
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.update()
            .where(ACTIVATIONS.c.activated_at == stamp(START))
            .values(plan_id="valued-plan")
        )
    runtime.process(contract(buy="0.4"), END + timedelta(hours=1))
    first = runtime.overview(START, END)
    assert first["coverage"]["evaluated"] == 1
    assert first["savings"] == pytest.approx(-0.4)
    assert first["totals"]["reference"]["inventory_change"] == 0


@pytest.mark.parametrize("role, entities", [("imports", ["sensor.pv"]), ("exports", [])])
def test_capture_cannot_reclassify_or_omit_grid_meters(settlement, role, entities):
    """A positive PV term is not a grid import meter, and configured export channels cannot disappear from billing."""
    runtime, _store, docs, _engine, _plans = settlement
    selected = bindings()
    pending = {
        "version": "grid-role-request",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "bindings": selected,
    }
    report = {
        "request_version": pending["version"],
        "frames": [frame().record()],
        "imports": selected["imports"],
        "exports": selected["exports"],
    }
    report[role] = entities
    with pytest.raises(ValueError, match="Grid settlement meters"):
        runtime.receive(pending, report, END, (), None)
    assert not docs.exists("capture.json")


def test_pending_capture_reuses_only_one_archived_binding_projection(settlement, monkeypatch):
    """Repeated polls reuse the immutable plan binding projection, not the full reconstructed 48-hour plan."""
    from types import SimpleNamespace

    runtime, _store, _docs, _engine, plans = settlement
    original = plans.get_plan
    read_ids = []

    def counted(plan_id):
        """Count expensive archive reconstructions while keeping real stored plan semantics."""
        read_ids.append(plan_id)
        return original(plan_id)

    monkeypatch.setattr(plans, "get_plan", counted)
    for _ in range(2):
        assert runtime.request(
            END + timedelta(hours=1),
            SimpleNamespace(batteries=()),
            SimpleNamespace(sources=()),
            SimpleNamespace(terms=()),
            (),
            None,
        )
    assert read_ids == ["original-plan"]


@pytest.mark.parametrize(
    "cuts",
    [
        (timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=15)),
        (timedelta(minutes=5),),
        (timedelta(minutes=15) - timedelta(microseconds=1), timedelta(minutes=15)),
    ],
    ids=["subquarters", "unfinished-prefix", "fractional-second"],
)
def test_capture_rejects_nonquarter_interior_without_poisoning_archive(settlement, cuts):
    """Off-grid interior or incomplete final frames must leave evidence/pointer unchanged. A correct retry succeeds."""
    from itertools import pairwise

    runtime, store, docs, _engine, _plans = settlement
    start = END + timedelta(minutes=15)
    end = start + timedelta(minutes=15)
    pending = {
        "version": "quarter-grid",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bindings": bindings(),
    }
    moments = (start, *(start + offset for offset in cuts))
    report = {
        "request_version": pending["version"],
        "imports": bindings()["imports"],
        "exports": bindings()["exports"],
        "frames": [
            replace(frame(start=left), end=right).record() for left, right in pairwise(moments)
        ],
    }
    with pytest.raises(ValueError, match="quarter boundary"):
        runtime.receive(pending, report, end, (), None)
    assert store.frames(after=start) == []
    assert not docs.exists("capture.json")
    report["frames"] = [frame(start=start).record()]
    runtime.receive(pending, report, end, (), None)
    assert len(store.frames(after=start)) == 1
    assert docs.read("capture.json")["end"] == end.isoformat()


@pytest.mark.parametrize(
    "start_minutes,end_minutes,cuts",
    [
        (0, 45, (15, 30, 45)),
        (5, 30, (15, 30)),
        (0, 20, (15, 20)),
        (5, 20, (15, 20)),
        (0, 45, (15, 30)),
    ],
    ids=["quarters", "partial-start", "partial-end", "both-edges", "whole-quarter-prefix"],
)
def test_capture_preserves_requested_configuration_edges(
    settlement, start_minutes, end_minutes, cuts
):
    """Only requested first/last boundaries may split quarters. Complete quarter prefixes may advance safely."""
    from itertools import pairwise

    runtime, store, docs, _engine, _plans = settlement
    origin = END + timedelta(minutes=15)
    start, end = origin + timedelta(minutes=start_minutes), origin + timedelta(minutes=end_minutes)
    moments = (start, *(origin + timedelta(minutes=offset) for offset in cuts))
    pending = {
        "version": "boundary-grid",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bindings": bindings(),
    }
    rows = [
        replace(
            frame(start=left),
            end=right,
            quality="estimated_boundary" if right - left < timedelta(minutes=15) else "recorded",
        ).record()
        for left, right in pairwise(moments)
    ]
    report = {
        "request_version": pending["version"],
        "imports": bindings()["imports"],
        "exports": bindings()["exports"],
        "frames": rows,
    }
    runtime.receive(pending, report, end, (), None)
    assert len(store.frames(after=start)) == len(rows)
    assert docs.read("capture.json")["end"] == moments[-1].isoformat()


@pytest.mark.parametrize("seed_kind", ["missing", "aggregate"])
def test_bonus_failure_does_not_stop_physical_reference(settlement, seed_kind):
    """Two measured 1 kWh quarters deplete the independent 2 kWh stock despite an unknown bonus budget."""
    runtime, _store, docs, engine, plans = settlement
    original = plans.get_plan("original-plan")
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.savings.forecast_state import initial_state

    selected = contract(bonus=True)
    book = None
    if seed_kind != "missing":
        book = CounterBook(
            END,
            (
                CounterSeed(
                    "bonus",
                    selected.bonuses[0].period_start(START),
                    1,
                    10,
                    "sensor.export" if seed_kind == "late" else "sensor.aggregate",
                    END if seed_kind == "late" else START,
                    () if seed_kind == "late" else (("sensor.export", 10),),
                ),
            ),
        )
    runtime.process(selected, END + timedelta(hours=1), book)
    assert runtime.issue == ("Historical bonus baseline is being read from Home Assistant")
    state = docs.read("physical-reference.json")
    assert state["reference_stored"] == {"battery": 0}
    assert state["actual_stored"] == {"battery": 2}
    assert state["end"] == (END + timedelta(minutes=15)).isoformat()
    restarted = SavingsRuntime(SettlementStore(engine), SqlDocuments(engine, "savings"), plans)
    document = plans.get_plan("original-plan")
    document["input_snapshot"]["problem"]["intervals"][0]["start"] = state["end"]
    continued = initial_state(_store, docs, plans, document, selected)
    assert continued["stored"] == {"battery": 0}
    assert continued["counters"] == {}
    assert continued["status"] == "continuous_reference"
    restarted.process(contract(buy="0.4", bonus=True), END + timedelta(hours=2), book)
    assert docs.read("physical-reference.json") == state
    assert plans.get_plan("original-plan") == original


def test_later_budget_confirmation_initializes_historical_billing_on_every_backend(
    settlement, tmp_path
):
    """A later 1-kWh confirmation at the unchanged export meter reconstructs both old quarters without changing evidence."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed

    runtime, store, _docs, engine, _plans = settlement
    selected = contract(bonus=True)
    original = store.frames()
    seed = CounterSeed(
        "bonus", selected.bonuses[0].period_start(START), 1, 10, "sensor.export", END
    )
    confirmed = CounterBook(END, (seed,))
    runtime.process(selected, END + timedelta(hours=1), confirmed)
    assert runtime.issue is None
    result = runtime.overview(START, END + timedelta(minutes=15))
    assert result["coverage"]["evaluated"] == 2
    assert store.frames() == original
    assert confirmed.seeds == (seed,)
    # An automatic historical seed lives separately from the manual planning confirmation.
    runtime.bonus.archive.save(confirmed)
    from open_energy_planner.storage.counters import CounterStore

    current = CounterStore(SqlDocuments(engine, "bonus-counters"))
    current.save(CounterBook(END, (replace(seed, used_kwh=9),)))
    assert runtime.bonus.archive.current == confirmed
    assert current.current.seeds[0].used_kwh == 9


def test_historical_source_confirmation_survives_a_current_meter_change(settlement, monkeypatch):
    """A new physical meter's 99-kWh confirmation cannot replace the retained old source's 1-kWh opening volume."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed

    runtime, store, docs, _engine, _plans = settlement
    selected = contract(bonus=True)
    original = store.frames
    seed = CounterSeed(
        "bonus", selected.bonuses[0].period_start(START), 1, 10, "sensor.export", START
    )
    record = json.loads(json.dumps(asdict(seed), default=lambda value: value.isoformat()))

    def historical_context(*args, **kwargs):
        """Supply the archived old confirmation without changing any stored measurement row."""
        rows = original(*args, **kwargs)
        for row in rows:
            row["context"]["seeds"] = [record]
        return rows

    monkeypatch.setattr(store, "frames", historical_context)
    new_source = CounterBook(END, (replace(seed, entity_id="sensor.replacement", used_kwh=99),))
    runtime.process(selected, END + timedelta(hours=1), new_source)
    assert runtime.issue is None
    state = store.state(docs.read("head.json")["revision"])
    assert list(state["counters"]["actual"].values()) == [1]
    assert list(state["counters"]["reference"].values()) == [1]


def test_multiple_export_anchors_backdate_on_every_database_backend(settlement):
    """Both physical meters contribute .50+.50+.15 kWh, leaving 8.85 of the confirmed 10 kWh at the old boundary."""
    from tests.app.savings.test_savings_bonus import EXPORTS, anchor, measured_rows

    runtime, store, _docs, _engine, _plans = settlement
    shift = timedelta(hours=2)
    original = store.frames()
    for measured, context in measured_rows():
        store.put_frames(
            (replace(measured, start=measured.start + shift, end=measured.end + shift),), context
        )
    confirmed = replace(anchor(), observed_at=anchor().observed_at + shift)
    seed = runtime.bonus.backdate(confirmed, START + shift, EXPORTS)
    assert seed.used_kwh == pytest.approx(8.85)
    assert seed.component_meters == ((EXPORTS[0], 100), (EXPORTS[1], 200))
    assert store.frames()[:2] == original


def test_missing_physical_interval_does_not_reset_or_skip_reference(settlement):
    """After two measured quarters a missing third blocks a fourth. Budget failure never permits an SoC reset."""
    runtime, store, docs, _engine, _plans = settlement
    runtime.process(contract(bonus=True), END + timedelta(hours=1))
    before = docs.read("physical-reference.json")
    store.put_frames(
        (frame(start=END + timedelta(minutes=30)),),
        {"bindings": bindings(), "seeds": [], "location": None},
    )
    runtime.process(contract(bonus=True), END + timedelta(hours=2))
    assert docs.read("physical-reference.json") == before
    assert runtime.physical_issue == "Reference continuity requires the missing interval first"


def test_physical_reference_advances_without_contract(settlement):
    """Measured physics requires no tariff configuration or inventory valuation to continue its stock."""
    runtime, _store, docs, _engine, _plans = settlement
    runtime.process(None, END + timedelta(hours=1))
    assert docs.read("physical-reference.json")["reference_stored"] == {"battery": 0}
    assert runtime.issue == "Contract configuration is required for settlement"
    assert runtime.physical_issue is None


@pytest.mark.parametrize(
    "stocks,aligned",
    [
        ({"battery": 0.00000005}, True),
        ({"battery": 0.0000002}, False),
        ({"different-battery": 0}, False),
    ],
)
def test_reference_budget_alignment_uses_absolute_energy_tolerance(
    settlement, monkeypatch, stocks, aligned
):
    """A .00000005 kWh replay difference retains quota. A .0000002 difference or different IDs cannot borrow it."""
    from open_energy_planner.savings.forecast_state import initial_state

    runtime, store, docs, _engine, plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    financial = store.state(docs.read("head.json")["revision"])
    financial["counters"]["reference"] = {"retained-opening-budget": 1.25}
    monkeypatch.setattr(store, "state", lambda revision: financial)
    physical = docs.read("physical-reference.json")
    physical["reference_stored"] = stocks
    docs.write("physical-reference.json", physical)
    document = plans.get_plan("original-plan")
    document["input_snapshot"]["problem"]["intervals"][0]["start"] = physical["end"]
    result = initial_state(store, docs, plans, document, contract())
    assert result["counters"] == ({"retained-opening-budget": 1.25} if aligned else {})
    assert result["stored"] == stocks


@pytest.mark.parametrize("changed", [False, True])
def test_new_plan_counter_provenance_cannot_borrow_old_financial_quota(settlement, changed):
    """Matching provenance retains 1 kWh. A new 2 kWh baseline cannot borrow it before background replay."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.savings.forecast_state import initial_state

    runtime, store, docs, _engine, plans = settlement
    selected = contract(bonus=True)
    seed = CounterSeed(
        "bonus", selected.bonuses[0].period_start(START), 1, 10, "sensor.export", START
    )
    old_book = CounterBook(START, (seed,))
    runtime.process(selected, END + timedelta(hours=1), old_book)
    assert runtime.issue is None
    assert docs.read("working.json") is None
    replacement = CounterBook(END, (replace(seed, used_kwh=2),))
    document = plans.get_plan("original-plan")
    document["input_snapshot"]["problem"]["intervals"][0]["start"] = (
        END + timedelta(minutes=15)
    ).isoformat()
    document["input_snapshot"]["bonus_evidence"] = [
        {"counter_version": replacement.version if changed else old_book.version}
    ]
    result = initial_state(store, docs, plans, document, selected)
    assert result["stored"] == {"battery": 0}
    assert result["counters"] == ({} if changed else {"bonus|2026-01-01T00:00:00+00:00": 1})
    assert result["evaluation_revision"] == (
        None if changed else docs.read("head.json")["revision"]
    )
