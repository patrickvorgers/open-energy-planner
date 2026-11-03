"""Frozen-command replay protects financial model selection from hindsight dispatch."""

from dataclasses import replace

import pytest

from open_energy_planner.optimization.adapters.highs.replay import dispatch
from tests.app.installation.test_installation import problem


def test_empty_battery_replay_imports_actual_demand():
    """A requested 1 kWh discharge cannot manufacture energy. The 1 kWh load is imported."""
    inputs = problem(prices=(5,), loads=(1,), pv=(0,), initial=0, hybrid=True)
    flow, wear = dispatch(
        inputs,
        inputs.intervals[0],
        {"battery": 0},
        {"battery": {"mode": "fixed_power", "power_w": 1000}},
    )
    assert flow.grid_import == pytest.approx(1)
    assert flow.batteries[0].discharge_kwh == pytest.approx(0)
    assert flow.batteries[0].end_kwh == pytest.approx(0)
    assert wear == 0


def test_fixed_replay_does_not_arbitrage_realized_price():
    """An idle command stays idle at either price. Actual prices never enter physical recourse."""
    for price in (-5, 5):
        inputs = problem(prices=(price,), loads=(1,), pv=(0,), initial=1, hybrid=True)
        flow, _ = dispatch(
            inputs,
            inputs.intervals[0],
            {"battery": 1},
            {"battery": {"mode": "fixed_power", "power_w": 0}},
        )
        assert flow.grid_import == pytest.approx(1)
        assert flow.batteries[0].end_kwh == pytest.approx(1)


def test_grid_following_reacts_only_to_current_realized_load():
    """A grid-following battery covers the current 1 kWh load within its lossless limits."""
    inputs = problem(prices=(5,), loads=(1,), pv=(0,), initial=1, hybrid=True)
    inputs = replace(inputs, batteries=(replace(inputs.batteries[0], can_follow_grid=True),))
    flow, _ = dispatch(
        inputs,
        inputs.intervals[0],
        {"battery": 1},
        {"battery": {"mode": "follow_grid_target", "power_w": None}},
    )
    assert flow.grid_import == pytest.approx(0)
    assert flow.grid_export == pytest.approx(0)
    assert flow.batteries[0].end_kwh == pytest.approx(0)


@pytest.mark.parametrize("stage", ("deadline", "reserve", "native"))
def test_replay_timeouts_are_retryable(monkeypatch, stage):
    """Every exhausted native stage raises TimeoutError, preserving recorded evidence for retry."""
    from open_energy_planner.optimization.adapters.highs import replay

    inputs = problem(prices=(5,), loads=(1,), pv=(0,), initial=1, hybrid=True)
    if stage == "deadline":
        ticks = iter((0, 3))
        monkeypatch.setattr(replay, "perf_counter", lambda: next(ticks))
    elif stage == "reserve":
        monkeypatch.setattr(
            replay, "prioritize_reserve", lambda *_args: "reserve_recovery_time_limit"
        )
    else:
        monkeypatch.setattr(
            replay.highspy.Highs,
            "getModelStatus",
            lambda _native: replay.highspy.HighsModelStatus.kTimeLimit,
        )
    with pytest.raises(TimeoutError, match="time limit"):
        dispatch(
            inputs,
            inputs.intervals[0],
            {"battery": 1},
            {"battery": {"mode": "fixed_power", "power_w": 0}},
        )
