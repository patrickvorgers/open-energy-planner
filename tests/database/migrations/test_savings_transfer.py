"""Transfer retained settlement evidence without losing original plans or counter origins."""

from dataclasses import replace
from datetime import timedelta

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame


def test_reconstructed_soc_quality_survives_restart_and_backend_transfer(settlement, tmp_path):
    """Estimated SoC evidence remains immutable and labelled after restart and transfer on all four backends."""
    from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive

    runtime, store, docs, engine, plans = settlement
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
    target = archive_engine(tmp_path / "estimated-soc-target")
    try:
        migrate_archive(engine, target, overwrite_target=True)
        assert SettlementStore(target).frames() == restarted.frames()
        assert SqlDocuments(target, "savings").read("physical-reference.json") == docs.read(
            "physical-reference.json"
        )
        assert SqlPlanRepository(tmp_path / "estimated-soc-target", engine=target).get_plan(
            "original-plan"
        ) == plans.get_plan("original-plan")
    finally:
        target.dispose()


def test_settlement_transfers_with_every_archive_table(settlement, tmp_path):
    """Moving any supported backend into SQLite retains meters, evaluations, continuation and original plans."""
    from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive

    runtime, _store, _docs, engine, plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    before = runtime.overview(START, END + timedelta(minutes=15))
    target_directory = tmp_path / "settlement-target"
    target = archive_engine(target_directory)
    try:
        migrate_archive(engine, target, overwrite_target=True)
        restored_plans = SqlPlanRepository(target_directory, engine=target)
        restored = SavingsRuntime(
            SettlementStore(target), SqlDocuments(target, "savings"), restored_plans
        )
        assert restored.documents.read("physical-reference.json") == runtime.documents.read(
            "physical-reference.json"
        )
        restored.process(contract(), END + timedelta(hours=2))
        assert restored.overview(START, END + timedelta(minutes=15)) == before
        assert restored_plans.get_plan("original-plan") == plans.get_plan("original-plan")
        previous_cursor = restored.store.price_cursor("NL", "EUR")
        with target.begin() as connection:
            retain(
                connection,
                (
                    PriceSeries(
                        "after-transfer",
                        "NL",
                        "EUR",
                        START,
                        None,
                        None,
                        (PricePoint(START, END, 0.7, PriceKind.PUBLISHED, 15),),
                    ),
                ),
            )
        assert restored.store.price_cursor("NL", "EUR") > previous_cursor
        restored.process(contract(), END + timedelta(hours=3))
        assert restored.overview(START, END)["revision"] != before["revision"]
    finally:
        target.dispose()


def test_later_budget_confirmation_survives_backend_transfer(settlement, tmp_path):
    """A later 1-kWh confirmation at the unchanged export meter reconstructs both old quarters without changing evidence."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive

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
    target = archive_engine(tmp_path / "bonus-history-target")
    try:
        migrate_archive(engine, target, overwrite_target=True)
        moved = SavingsRuntime(
            SettlementStore(target),
            SqlDocuments(target, "savings"),
            SqlPlanRepository(tmp_path / "bonus-history-target", engine=target),
        )
        assert moved.bonus.archive.current == confirmed
        assert CounterStore(SqlDocuments(target, "bonus-counters")).current.seeds[0].used_kwh == 9
        assert SettlementStore(target).frames() == original
        assert moved.overview(START, END + timedelta(minutes=15)) == result
    finally:
        target.dispose()
