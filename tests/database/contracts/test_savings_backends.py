"""Settlement continuity, corrections and complete evidence on every advertised database backend."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame
from tests.database.support.history_datasets import retained_evaluation, settlement_batches


def test_reconstructed_soc_quality_survives_restart(settlement, tmp_path):
    """Estimated SoC evidence remains immutable and labelled after restart on all four backends."""

    _runtime, store, _docs, engine, _plans = settlement
    original = store.frames()
    reconstructed = replace(frame(start=END + timedelta(minutes=15)), quality="estimated_soc")
    context = {"bindings": bindings(), "seeds": [], "location": None}
    store.put_frames((reconstructed,), context)
    restarted = SettlementStore(engine)
    assert restarted.frames()[:2] == original
    assert restarted.frames()[-1]["frame"]["quality"] == "estimated_soc"


def test_ingestion_retries_do_not_duplicate_or_replace_meter_evidence(settlement):
    """Identical evidence is idempotent. Conflicting meter totals never silently rewrite evaluated history."""
    _runtime, store, _docs, _engine, _plans = settlement
    context = {"bindings": bindings(), "seeds": [], "location": None}
    store.put_frames((frame(),), context)
    assert len(store.frames()) == 2
    with pytest.raises(ValueError, match="immutable"):
        store.put_frames((replace(frame(), channels={"sensor.import": 2}),), context)
    assert store.frames()[0]["frame"]["channels"]["sensor.import"] == 1


def test_empty_scanned_batch_advances_without_inventing_measurements(settlement):
    """A whole unavailable day can be passed without zero frames or costs being fabricated."""
    runtime, store, documents, _engine, _plans = settlement
    start = END + timedelta(minutes=15)
    end = start + timedelta(days=1)
    pending = {
        "version": "empty",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bindings": bindings(),
    }
    runtime.receive(
        pending,
        {
            "request_version": "empty",
            "imports": ["sensor.import"],
            "exports": ["sensor.export"],
            "frames": [],
            "scanned_until": end.isoformat(),
        },
        end,
        (),
        None,
    )
    assert documents.read("capture.json") == {"end": end.isoformat()}
    assert len(store.frames()) == 2


def test_recovery_receipt_commits_frames_and_both_cursors_atomically(settlement, monkeypatch):
    """Failure writing recovery progress rolls back new frames and the forward cursor on every backend."""
    runtime, store, documents, _engine, _plans = settlement
    start, end = END + timedelta(minutes=15), END + timedelta(minutes=30)
    documents.write("capture.json", {"end": end.isoformat()})
    pending = {
        "version": "atomic-recovery",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bindings": bindings(),
    }
    response = {
        "request_version": pending["version"],
        "imports": ["sensor.import"],
        "exports": ["sensor.export"],
        "frames": [frame(start=start).record()],
        "scanned_until": end.isoformat(),
    }
    original_write = documents.write

    def fail_recovery(name, value):
        """Fail only the final recovery-cursor write after the earlier transaction writes have executed."""
        if name == "recovery.json":
            raise OSError("Synthetic recovery storage failure")
        return original_write(name, value)

    with monkeypatch.context() as patch:
        patch.setattr(documents, "write", fail_recovery)
        with pytest.raises(OSError, match="Synthetic recovery storage failure"):
            runtime.receive(pending, response, end, (), None)
    assert len(store.frames()) == 2
    assert documents.read("capture.json") == {"end": end.isoformat()}
    assert not documents.exists("recovery.json")
    runtime.receive(pending, response, end, (), None)
    assert len(store.frames()) == 3
    assert documents.read("capture.json") == {"end": end.isoformat()}
    assert documents.read("recovery.json") == {"end": end.isoformat()}


@pytest.mark.parametrize(
    "damage", ["future", "outside", "naive", "off_grid", "overlap", "before", "after"]
)
def test_sparse_capture_rejects_invalid_scan_or_frame_boundaries_atomically(settlement, damage):
    """Sparse transport never permits future, overlapping, off-grid or out-of-request evidence."""
    runtime, store, documents, _engine, _plans = settlement
    start = END + timedelta(minutes=15)
    end = start + timedelta(hours=1)
    scanned = end
    now = end
    captured = [frame(start=start).record()]
    if damage == "future":
        now = end - timedelta(minutes=15)
    elif damage == "outside":
        scanned += timedelta(minutes=15)
    elif damage == "naive":
        scanned = scanned.replace(tzinfo=None)
    elif damage == "off_grid":
        captured = [frame(start=start + timedelta(minutes=1)).record()]
    elif damage == "overlap":
        captured *= 2
    elif damage == "before":
        captured = [frame(start=start - timedelta(minutes=15)).record()]
    else:
        captured = [frame(start=end).record()]
    pending = {
        "version": "invalid",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bindings": bindings(),
    }
    with pytest.raises(ValueError):
        runtime.receive(
            pending,
            {
                "request_version": "invalid",
                "imports": ["sensor.import"],
                "exports": ["sensor.export"],
                "frames": captured,
                "scanned_until": scanned.isoformat(),
            },
            now,
            (),
            None,
        )
    assert len(store.frames()) == 2
    assert not documents.exists("capture.json")


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


def test_background_repricing_skips_current_contract_without_loading_plans(settlement, monkeypatch):
    """A current-contract queue entry advances without reconstructing its original plan."""
    from open_energy_planner.savings.repricing import PlanValuations
    from tests.app.savings.test_plan_repricing import original_plan

    _runtime, _store, docs, _engine, plans = settlement
    original = original_plan()
    original["input_snapshot"]["contract_version"] = contract().version
    plans.state["plan"] = original
    plans.save()

    read_original = plans.get_plan

    def forbidden_read(identity):
        """Fail if a queue skip reconstructs the entire immutable plan."""
        assert identity != original["plan_id"], "Current-contract plan must not be reconstructed"
        return read_original(identity)

    monkeypatch.setattr(plans, "get_plan", forbidden_read)
    PlanValuations(docs, plans).process(contract(), None, END)
    assert docs.read("repricing.json")["complete"] is True


def test_corrupt_contract_leaf_does_not_block_other_plan_values(settlement):
    """A corrupt newest plan keeps its prior value while the healthy plan receives the new rate."""
    from copy import deepcopy

    from open_energy_planner.savings.repricing import PlanValuations
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import VALUES, digest
    from tests.app.savings.test_plan_repricing import original_plan

    _runtime, _store, docs, engine, plans = settlement
    healthy = original_plan()
    healthy["plan_id"] = "healthy-plan"
    healthy["input_snapshot"]["contract_version"] = "healthy-original-contract"
    plans.state["plan"] = healthy
    plans.save()
    broken = deepcopy(healthy)
    broken["plan_id"] = "broken-plan"
    broken["generated_at"] = (START + timedelta(minutes=1)).isoformat()
    broken["input_snapshot"]["contract_version"] = "broken-original-contract"
    plans.state["plan"] = broken
    plans.save()
    values = PlanValuations(docs, plans)
    values.process(contract(buy="0.4"), None, END)
    accepted = values.latest("broken-plan")["valuation"]
    with engine.begin() as connection:
        connection.execute(
            VALUES.update()
            .where(VALUES.c.digest == digest("broken-original-contract"))
            .values(payload='"corrupt"')
        )
    values.process(contract(buy="0.5"), None, END + timedelta(hours=1))
    assert values.latest("broken-plan")["valuation"] == accepted
    assert values.latest("broken-plan")["valuation_status"]["issue"]
    assert values.latest("healthy-plan")["valuation"]["ems_costs"]["purchase"] == pytest.approx(0.5)
    assert docs.read("repricing.json")["complete"] is True


def test_sparse_multi_year_query_preserves_year_bounds_and_bounded_details(settlement):
    """Ninety-seven quarters inside 2023 exclude both neighbors and retain the detail and calendar limits."""
    from datetime import UTC, datetime

    from open_energy_planner.savings.storage.adapters.sqlalchemy import (
        SETTLEMENT_EVALUATIONS,
        SETTLEMENT_FRAMES,
    )

    runtime, _store, docs, engine, _plans = settlement
    head, sample = retained_evaluation(docs)
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
    original = plans.get_context
    read_ids = []

    def counted(plan_id):
        """Count compact context reads while keeping real stored projection semantics."""
        read_ids.append(plan_id)
        return original(plan_id)

    monkeypatch.setattr(plans, "get_context", counted)
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
    result = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", selected, confirmed, END + timedelta(days=1), START, None
    )
    assert runtime.issue is None
    assert result["cash"]["coverage_seconds"] == 1800
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

    runtime, store, _docs, _engine, _plans = settlement
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
    result = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", selected, new_source, END + timedelta(days=1), START, None
    )
    assert runtime.issue is None
    assert result["cash"]["coverage_seconds"] == 1800
    assert result["scenarios"]["actual"]["totals"]["bonus"] == 0


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


def test_repricing_failure_hides_private_exception_values(settlement, monkeypatch, caplog):
    """Private pricing evidence cannot enter returned status, logs or overwrite the successful valuation."""
    from open_energy_planner.savings import repricing
    from tests.app.savings.test_plan_repricing import original_plan

    _runtime, _store, docs, _engine, plans = settlement
    original = original_plan()
    plans.state["plan"] = original
    plans.save()
    values = repricing.PlanValuations(docs, plans)
    values.process(contract(buy="0.4"), None, END)
    accepted = values.latest(original["plan_id"])["valuation"]

    def reject(*args, **kwargs):
        """Simulate an untrusted retained value without changing the monetary oracle."""
        raise ValueError("PRIVATE_REPRICING_TOKEN_AND_ARCHIVE_VALUE")

    monkeypatch.setattr(repricing, "reprice", reject)
    values.process(contract(buy="0.5"), None, END + timedelta(hours=1))
    result = values.latest(original["plan_id"])
    assert result["valuation"] == accepted
    assert "PRIVATE_REPRICING_TOKEN_AND_ARCHIVE_VALUE" not in json.dumps(result)
    assert "PRIVATE_REPRICING_TOKEN_AND_ARCHIVE_VALUE" not in caplog.text
    diagnostics = [row for row in caplog.records if row.name.endswith("savings.repricing")]
    assert diagnostics and all(row.exc_info is None for row in diagnostics)
    assert "ValueError" in diagnostics[0].getMessage()


def test_repricing_activity_identifies_plan_period_and_preserves_safe_failure(
    settlement, monkeypatch
):
    """Real stored activities identify the original horizon and explain financial failure without changing advice."""

    from open_energy_planner.activities.events import Events
    from open_energy_planner.savings import repricing
    from open_energy_planner.storage.adapters.activities import ActivityStore
    from tests.app.savings.support.scenarios import START
    from tests.app.savings.test_plan_repricing import original_plan

    async def scenario():
        """Keep both real attempts and the prior successful estimate when the next generation fails."""
        _runtime, _store, docs, engine, plans = settlement
        original = original_plan()
        plans.state["plan"] = original
        plans.save()
        list_plans = plans.list_plans

        def selected_plans(**options):
            """Isolate the fully valued archive while preserving real repository reads and writes."""
            return [row for row in list_plans(**options) if row["plan_id"] == original["plan_id"]]

        monkeypatch.setattr(plans, "list_plans", selected_plans)
        events = Events(ActivityStore(engine), lambda: END)
        events.loop = asyncio.get_running_loop()
        values = repricing.PlanValuations(docs, plans)
        values.events = events
        try:
            await asyncio.to_thread(values.process, contract(buy="0.4"), None, END)
            accepted = values.latest(original["plan_id"])["valuation"]

            def reject(*args, **kwargs):
                """Private archived evidence is never suitable activity guidance."""
                raise ValueError("PRIVATE_ACTIVITY_REPRICING_EVIDENCE")

            monkeypatch.setattr(repricing, "reprice", reject)
            changed = contract(buy="0.5")
            await asyncio.to_thread(values.process, changed, None, END)
            await asyncio.to_thread(values.process, changed, None, END)
            await events.flush()
            rows = events.store.read(END - timedelta(days=1), END + timedelta(days=1))
            assert len(rows) == 2
            failed = next(row for row in rows if row["state"] == "failed")
            assert failed["context"] == {
                "label": "Archived plan",
                "plan_id": "repricing-source",
                "plan_generated_at": START.isoformat(),
                "plan_strategy": "minimize cost",
                "start": START.isoformat(),
                "end": END.isoformat(),
            }
            assert (
                "does not change the active energy plan or its device commands" in failed["detail"]
            )
            assert "under Planning" in failed["detail"] and "Contract settings" in failed["detail"]
            assert "PRIVATE_ACTIVITY_REPRICING_EVIDENCE" not in json.dumps(rows)
            assert values.latest(original["plan_id"])["valuation"] == accepted
            assert plans.get_plan(original["plan_id"]) == original
        finally:
            await events.close()

    asyncio.run(scenario())


def test_old_repricing_failure_status_is_sanitized_without_rewriting_evidence(settlement):
    """Previously retained private failure values stay out of current projections without changing archives."""
    from open_energy_planner.savings.repricing import PlanValuations, key

    _runtime, _store, docs, _engine, plans = settlement
    name = "valueissue-" + key("synthetic-plan") + ".json"
    retained = {"issue": "PRIVATE_RETAINED_FAILURE", "contract_version": "retained-version"}
    docs.write(name, retained)
    result = PlanValuations(docs, plans).latest("synthetic-plan")
    assert "PRIVATE_RETAINED_FAILURE" not in json.dumps(result)
    assert result["valuation_status"]["contract_version"] == "retained-version"
    assert docs.read(name) == retained
