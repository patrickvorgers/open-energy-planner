"""Retain immutable opening provenance without relabelling legacy counters or resetting usage."""

from dataclasses import replace
from datetime import timedelta

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.contracts.bonus_history import BonusHistory
from open_energy_planner.contracts.bonus_statistics import derive_statistics, parse_statistics
from open_energy_planner.storage.counters import CounterStore
from tests.app.history.support.bonus import NOW, START, inputs
from tests.app.history.test_bonus_statistics import report
from tests.database.support.archive import make_store, read_document


def test_legacy_origin_stays_unknown_and_corrupt_metadata_cannot_change_budget(tmp_path):
    """An old 12.5-kWh opening retains its exact hash. Bad explanatory metadata cannot invent an origin."""
    store = make_store(tmp_path, CounterStore)
    seed = CounterSeed("solar", START, 12.5, 5, "sensor.export", NOW)
    book = CounterBook(NOW, (seed,))
    store.save(book)
    original = read_document(tmp_path, "bonus-counters", f"{book.version}.json")
    assert store.provenance(seed) is None
    name, _identity = store._origin_identity(seed)
    store.documents.write(name, {"source": "home_assistant_history"})
    assert store.provenance(seed) is None
    assert store.load(book.version) == book
    assert read_document(tmp_path, "bonus-counters", f"{book.version}.json") == original


def test_price_revision_continues_usage_but_reset_and_new_supplier_request_new_period(tmp_path):
    """Changed terms keep eight measured kWh. A new year or supplier requests its own history without changing the old book."""
    store = make_store(tmp_path, CounterStore)
    contract, configuration = inputs()
    service = BonusHistory()
    request = service.request(contract, configuration, None, NOW)
    book = derive_statistics(request, parse_statistics(report(request), request), None, NOW)
    store.save_statistics(book)
    origin = store.provenance(book.seeds[0])
    assert origin is not None and origin["source"] == "home_assistant_history"
    later = START + timedelta(days=1)
    changed = replace(
        contract,
        bonuses=contract.bonuses
        + (replace(contract.bonuses[0], cap_kwh=150, effective_from=later),),
    )
    refresh = service.request(changed, configuration, book, later)
    assert refresh.periods == (("solar", START),)
    body = report(refresh)
    cursor = book.seeds[0].observed_at
    # The first four hours exported 8 kWh. All later hours exported nothing.
    while cursor < refresh.end:
        body["statistics"][0]["hours"].append([cursor.timestamp(), 108, 1])
        cursor += timedelta(hours=1)
    refreshed = derive_statistics(refresh, parse_statistics(body, refresh), book, later)
    assert refreshed.seeds[0].statistics.used(later) == 8
    assert refreshed.seeds[0].statistics.reference_at == START
    assert refreshed.seeds[0].statistics.latest_at == refresh.end
    new_year = service.request(changed, configuration, book, NOW.replace(year=2027))
    assert new_year.periods == (("solar", START.replace(year=2027)),)
    switched = replace(
        changed, bonuses=(replace(changed.bonuses[-1], rule_id="next", contract_start=later),)
    )
    new_supplier = service.request(switched, configuration, book, later + timedelta(hours=5))
    assert new_supplier.periods == (("next", later),)
    assert store.current == book
    assert store.provenance(book.seeds[0]) == origin
