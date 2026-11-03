"""Verify conserved simulator energy and exact repair proposals without starting Home Assistant."""

from datetime import datetime, timedelta

import pytest


@pytest.mark.parametrize("exported", [2.0, 0.3, 0.0])
def test_missing_simulated_cycle_repair_conserves_grid_and_is_idempotent(exported):
    """The .20-kWh fixture baseline plus .83 cycle is 1.03. Reduce export first, leaving .16 after selected use."""
    from datetime import UTC

    from test_environment.oep_test_fixture.calculation.recorder.balance import proposal

    start = datetime(2026, 10, 24, 12, tzinfo=UTC).timestamp()
    roles = {"import": "grid_import", "export": "grid_export", "pv": "solar"}
    deltas = {
        "import": max(0, 0.2 - exported),
        "export": exported,
        "heat": 0.04,
        "dish": 0.83,
        "ev": 0,
    }
    # Independently set PV so that the observed household balance is exactly .2 kWh.
    deltas["pv"] = 0.2 - deltas["import"] + exported
    statistics = {
        entity: [
            {"start": start, "sum": 10},
            {"start": start + 3600, "sum": 10 + amount},
            {"start": start + 7200, "sum": 10 + amount},
        ]
        for entity, amount in deltas.items()
    }
    repairs = proposal(statistics, roles, ["heat", "dish", "ev"], {"dish", "ev"})
    assert len(repairs) == 1
    repair = repairs[0]
    assert repair["expected_kwh"] == pytest.approx(1.03)
    assert repair["expected_kwh"] - repair["selected_kwh"] == pytest.approx(0.16)
    assert repair["reason"] == "missing_controlled_energy"
    for row in statistics["import"][1:]:
        row["sum"] += 0.83
    assert proposal(statistics, roles, ["heat", "dish", "ev"], {"dish", "ev"}) == []


def test_simulated_balance_repair_rejects_reset_instead_of_rewriting_other_evidence():
    """An unexplained negative .5-kWh cumulative delta stops repair rather than being treated as device use."""
    from test_environment.oep_test_fixture.calculation.recorder.balance import proposal

    statistics = {"pv": [{"start": 0, "sum": 10}, {"start": 3600, "sum": 9.5}]}
    with pytest.raises(ValueError, match="Counter resets"):
        proposal(statistics, {"pv": "solar"}, [], set())


@pytest.mark.parametrize("missing_point", [None, "constant", "constant_gap", "changing"])
def test_fine_grid_repair_handles_two_connections_with_independent_origin_resets(missing_point):
    """Repair 50-kWh origin drops with full or proven-constant evidence, but reject a changing gap untouched.

    Each meter takes .3304167 import and .9154167 export. A missing dishwasher
    point or longer gap bounded by equal counters proves zero use. An unknown changing point
    remains unavailable. States are preserved and repeating a repair is a no-op.
    """
    from copy import deepcopy
    from datetime import UTC

    from test_environment.oep_test_fixture.calculation.recorder.balance import repair_documents

    start = datetime(2026, 10, 24, 13, tzinfo=UTC).timestamp()
    roles = {
        "import_a": "grid_import",
        "import_b": "grid_import",
        "export_a": "grid_export",
        "export_b": "grid_export",
        "pv": "solar",
        "battery": "battery_charge",
    }
    totals = {
        "import_a": 0,
        "import_b": 0,
        "export_a": 1,
        "export_b": 1,
        "pv": 2.2,
        "dish": 0.83,
        "heat": 0.04,
        "battery": 0,
    }
    hourly, fine = {}, {}
    for entity, energy in totals.items():
        reset = 50 if entity in roles and roles[entity].startswith("grid_") else 0
        hourly[entity] = [
            {"start": start - 3600, "sum": 100, "state": 100},
            {"start": start, "sum": 100 + energy - reset, "state": 100 + energy},
        ]
        fine[entity] = [{"start": start - 300, "sum": 500, "state": 100}]
        for part in range(1, 13):
            amount = energy if entity == "dish" else energy * part / 12
            fine[entity].append(
                {
                    "start": start + (part - 1) * 300,
                    "sum": 500 + amount - reset,
                    "state": 100 + amount,
                }
            )
    if missing_point == "constant_gap":
        del fine["dish"][2:8]
        del fine["battery"][2:8]
    elif missing_point:
        fine["dish"].pop(2 if missing_point == "constant" else 1)
    before = deepcopy(fine)
    if missing_point == "changing":
        with pytest.raises(ValueError, match="Complete five-minute evidence"):
            repair_documents(
                hourly,
                fine,
                roles,
                ["dish", "heat"],
                {"dish"},
                sample=lambda _: {"pv": 0, "battery": 0},
            )
        assert fine == before
        return
    repairs, writes = repair_documents(
        hourly, fine, roles, ["dish", "heat"], {"dish"}, sample=lambda _: {"pv": 0, "battery": 0}
    )
    assert len(repairs) == 1
    assert repairs[0]["adjustments"]["import_a"] == pytest.approx(50.33041666666667)
    assert repairs[0]["adjustments"]["export_b"] == pytest.approx(49.915416666666665)
    for period, target in (("hour", hourly), ("5minute", fine)):
        for entity, rows in writes[period].items():
            replacements = {row["start"]: row for row in rows}
            target[entity] = [replacements.get(row["start"], row) for row in target[entity]]
    for entity in roles:
        assert [row["state"] for row in fine[entity]] == [row["state"] for row in before[entity]]
    assert fine["import_a"][1]["sum"] - fine["import_a"][0]["sum"] == pytest.approx(
        0.3304166666666667
    )
    assert fine["export_b"][-1]["sum"] - fine["export_b"][0]["sum"] == pytest.approx(
        0.9154166666666666
    )
    assert fine["dish"] == before["dish"]
    for entity in ("import_a", "import_b", "export_a", "export_b"):
        assert fine[entity][-1]["sum"] == pytest.approx(hourly[entity][-1]["sum"])
    assert (
        repair_documents(
            hourly,
            fine,
            roles,
            ["dish", "heat"],
            {"dish"},
            sample=lambda _: {"pv": 0, "battery": 0},
        )[0]
        == []
    )


@pytest.mark.parametrize("resolution", ["hour", "5minute", "both", "spike"])
def test_simulated_source_origin_drop_preserves_states_and_zero_night_production(resolution):
    """Hourly, fine or combined 375-kWh night drops and a positive/negative spike all recover zero production.

    Recorded states remain unchanged. The spike case removes both sides of a
    false 375-kWh excursion rather than turning its positive side into generation.
    """
    from copy import deepcopy
    from datetime import UTC

    from test_environment.oep_test_fixture.calculation.recorder.balance import repair_documents

    start = datetime(2026, 10, 24, tzinfo=UTC).timestamp()
    hourly = {
        entity: [
            {"start": start - 3600, "sum": 100, "state": 100},
            {"start": start, "sum": 100 + energy, "state": 100 + energy},
        ]
        for entity, energy in (("import", 0.2), ("pv", 0), ("dish", 0))
    }
    fine = {
        entity: [
            {
                "start": start + (part - 1) * 300,
                "sum": 100 + energy * part / 12,
                "state": 100 + energy * part / 12,
            }
            for part in range(13)
        ]
        for entity, energy in (("import", 0.2), ("pv", 0), ("dish", 0))
    }
    for period, target in (("hour", hourly), ("5minute", fine)):
        if resolution not in (period, "both"):
            continue
        for row in target["pv"][1 if period == "hour" else 6 :]:
            row["sum"] -= 375
            row["state"] -= 375
    if resolution == "spike":
        fine["pv"][6]["sum"] += 375
        fine["pv"][6]["state"] += 375
    original = deepcopy({"hour": hourly, "5minute": fine})
    repairs, writes = repair_documents(
        hourly,
        fine,
        {"import": "grid_import", "pv": "solar"},
        ["dish"],
        {"dish"},
        sample=lambda _: {"pv": 0},
    )
    assert any(
        row["reason"] == "simulated_source_origin_discontinuity" and row["expected_kwh"] == 0
        for row in repairs
    )
    for period, target in (("hour", hourly), ("5minute", fine)):
        for entity, rows in writes[period].items():
            replacements = {row["start"]: row for row in rows}
            target[entity] = [replacements.get(row["start"], row) for row in target[entity]]
        assert [row["state"] for row in target["pv"]] == [
            row["state"] for row in original[period]["pv"]
        ]
        assert [row["sum"] for row in target["pv"]] == pytest.approx([100] * len(target["pv"]))
    assert (
        repair_documents(
            hourly,
            fine,
            {"import": "grid_import", "pv": "solar"},
            ["dish"],
            {"dish"},
            sample=lambda _: {"pv": 0},
        )[0]
        == []
    )


@pytest.mark.parametrize("second_hour", [1, 3])
def test_source_repairs_accumulate_across_hours_with_distinct_roof_profiles(second_hour):
    """Remove 50/70-kWh drops in adjacent or separated hours while roofs retain .8/.4 kWh per hour.

    A separate battery drop in hour two represents zero charging. Each source's
    expected cumulative total is its 100-kWh origin plus its independent hourly
    energy. Distinct roof profiles must not be replaced by a shared equal split.
    """
    from copy import deepcopy
    from datetime import UTC

    from test_environment.oep_test_fixture.calculation.recorder.balance import repair_documents

    start = datetime(2026, 10, 24, tzinfo=UTC).timestamp()
    energy = {"import": 0.2, "roof": 0.8, "garage": 0.4, "battery": 0, "dish": 0}
    roles = {
        "import": "grid_import",
        "roof": "solar",
        "garage": "solar",
        "battery": "battery_charge",
    }
    hourly = {
        entity: [
            {
                "start": start + (part - 1) * 3600,
                "sum": 100 + amount * part,
                "state": 100 + amount * part,
            }
            for part in range(6)
        ]
        for entity, amount in energy.items()
    }
    fine = {
        entity: [
            {
                "start": start + (part - 1) * 300,
                "sum": 100 + amount * part / 12,
                "state": 100 + amount * part / 12,
            }
            for part in range(61)
        ]
        for entity, amount in energy.items()
    }
    for target in (hourly, fine):
        for entity, drops in (
            ("roof", [(0, 50), (second_hour, 70)]),
            ("garage", [(2, 25)]),
            ("battery", [(2, 20)]),
        ):
            for row in target[entity]:
                loss = sum(amount for hour, amount in drops if row["start"] >= start + hour * 3600)
                row["sum"] -= loss
                row["state"] -= loss
    originals = deepcopy({"hour": hourly, "5minute": fine})

    def sample(_instant):
        """Independent quarter-kWh inputs, with distinct roof ratings and an idle battery."""
        return {"roof": 0.2, "garage": 0.1, "battery": 0}

    _, writes = repair_documents(hourly, fine, roles, ["dish"], {"dish"}, sample=sample)
    for period, target, width in (("hour", hourly, 1), ("5minute", fine, 12)):
        for entity, rows in writes[period].items():
            replacements = {row["start"]: row for row in rows}
            target[entity] = [replacements.get(row["start"], row) for row in target[entity]]
        for entity in ("roof", "garage", "battery"):
            assert [row["sum"] for row in target[entity]] == pytest.approx(
                [100 + energy[entity] * part / width for part in range(len(target[entity]))]
            )
            assert [row["state"] for row in target[entity]] == [
                row["state"] for row in originals[period][entity]
            ]
    assert repair_documents(hourly, fine, roles, ["dish"], {"dish"}, sample=sample)[0] == []


def test_simulated_quarters_vary_without_changing_hourly_energy_or_conservation():
    """A .2-kWh night hour is split .0425/.0525/.0575/.0475. Native five-minute endpoints sum to the same hour."""
    from datetime import UTC

    from test_environment.oep_test_fixture import quarter_energy
    from test_environment.oep_test_fixture.calculation.recorder.history import rows

    start = datetime(2026, 10, 23, tzinfo=UTC)
    samples = [quarter_energy(start + timedelta(minutes=15 * index)) for index in range(4)]
    assert [item["grid_import"] for item in samples] == pytest.approx(
        [0.0425, 0.0525, 0.0575, 0.0475]
    )
    assert all(
        item["grid_export"] == item["solar_roof"] == item["solar_garage"] == 0 for item in samples
    )
    hourly, fine, totals = rows(
        start,
        start + timedelta(hours=1),
        {"grid_import": 1000},
        lambda instant: {"grid_import": quarter_energy(instant)["grid_import"]},
    )
    assert len(hourly["grid_import"]) == 1
    assert len(fine["grid_import"]) == 12
    assert totals["grid_import"] == pytest.approx(1000.2)
    assert hourly["grid_import"][0]["sum"] == pytest.approx(1000.2)
    assert fine["grid_import"][2]["sum"] == pytest.approx(1000.0425)
    assert fine["grid_import"][2]["start"] + timedelta(minutes=5) == start + timedelta(minutes=15)


def test_unseeded_laundry_only_subtracts_complete_overlapping_history():
    """Return complete metered laundry deltas without requiring pre-creation history. Active gaps and resets remain unknown."""
    from test_environment.oep_test_fixture.calculation.consumption import recorded_additions

    anchors = {"washer": 3600, "dryer": 7200}
    series = {"washer": {3600: 50, 7200: 51, 10800: 51}, "dryer": {7200: 50, 10800: 51.4}}
    assert recorded_additions(series, anchors, 0, 3600) == 0
    assert recorded_additions(series, anchors, 3600, 7200) == 1
    assert recorded_additions(series, anchors, 7200, 10800) == pytest.approx(1.4)
    assert recorded_additions(series, anchors, 10800, 14400) is None
    assert recorded_additions(series, anchors, 1800, 5400) is None
    series["washer"][10800] = 49
    assert recorded_additions(series, anchors, 7200, 10800) is None


def test_native_calculations_do_not_initialize_home_assistant():
    """Native energy and detached Recorder math load without the HA framework or integration lifecycle."""
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from test_environment.oep_test_fixture.calculation.energy import quarter_energy; "
                "from test_environment.oep_test_fixture.calculation.recorder.balance import repair_documents; "
                "assert not any(name == 'homeassistant' or name.startswith('homeassistant.') for name in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, result.stdout + result.stderr
