"""Independent default and alternate-horizon checks without deriving test oracles from production constants."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from open_energy_planner.demand.calculation.model import horizon as demand_horizon
from open_energy_planner.planning_policy import planning_grid
from open_energy_planner.solar.calculation.model import horizon as solar_horizon


def test_shared_default_grid_has_96_quarters_and_24_hours_across_dst():
    """The agreed 48 elapsed hours remain exactly 120 decisions across Europe's DST change, shared by demand and solar."""
    now = datetime(2026, 10, 24, 23, 7, tzinfo=UTC)
    demand, solar = demand_horizon(now), solar_horizon(now)
    assert demand == solar
    assert len(demand) == 120
    assert all(duration == 0.25 for _, duration in demand[:96])
    assert all(duration == 1 for _, duration in demand[96:])
    assert demand[-1][0] + timedelta(hours=demand[-1][1]) == now.replace(minute=0) + timedelta(
        hours=48
    )


def test_alternate_policy_updates_actual_consumers_in_an_isolated_process():
    """A 60h planning and 84h price policy reaches real consumers without rewriting their source."""
    code = r"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import asyncio
import open_energy_planner.planning_policy as policy
policy.PLANNING_HORIZON_HOURS = 60
policy.PRICE_COVERAGE_HOURS = 84
from open_energy_planner.demand.calculation.model import horizon as demand
from open_energy_planner.demand.evaluation import evaluation_cutoffs
from open_energy_planner.solar.calculation.model import horizon as solar
from open_energy_planner.synthetic import capture
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries, combine
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.adapters.epex_predictor import EpexPredictor
from open_energy_planner.contracts.bonus import BonusRule
from open_energy_planner.contracts.calculation.formula import Formula
now = datetime(2026,10,22,tzinfo=UTC)
for grid in (demand(now),solar(now)):
    assert len(grid) == 132
    assert grid[-1][0]+timedelta(hours=grid[-1][1]) == now+timedelta(hours=60)
assert len(capture(now).problem.intervals) == 132
assert evaluation_cutoffs(now) == tuple(now-timedelta(hours=h) for h in (180,120,60))
prices = PriceSeries("test","NL","EUR",now,now,now+timedelta(hours=1),tuple(
    PricePoint(now+timedelta(hours=h),now+timedelta(hours=h+1),0.1,PriceKind.FORECAST,60) for h in range(84)))
assert combine((prices,),now,market_area="NL").requested_end == now+timedelta(hours=60)
service = PriceService("NL",None,None,SimpleNamespace(load=lambda:(),save=lambda _:None))
service.series = (prices,)
assert service.timeline(now).requested_end == now+timedelta(hours=60)
assert service.document(now)["selection_policy"]["price_coverage_hours"] == 84
rule = BonusRule("test",now,Formula("0"),1,"UTC")
rule.boundaries(now,now+timedelta(hours=60))
try:
    rule.boundaries(now,now+timedelta(hours=61))
except ValueError:
    pass
else:
    raise AssertionError("Bonus validation ignored the central horizon")
class Client:
    async def get(self,url,params):
        assert params["hours"] == "84"
        return {"knownUntil":(now-timedelta(hours=1)).isoformat(),"prices":[
            {"startsAt":(now+timedelta(hours=h)).isoformat(),"total":0.1} for h in range(85)]}
assert len(asyncio.run(EpexPredictor(Client(),"NL").fetch(now)).points) == 84
"""
    result = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unaligned_synthetic_grid_preserves_the_supplied_start():
    """Explicit synthetic captures preserve their exact input time while ordinary forecasts align to the current quarter."""
    now = datetime(2026, 10, 22, 0, 7, 15, tzinfo=UTC)
    assert planning_grid(now, align=False)[0][0] == now
    assert planning_grid(now)[0][0] == now.replace(minute=0, second=0)


@pytest.mark.parametrize(
    "minute,fine_count,tail", [(0, 96, 1), (15, 99, 0.25), (30, 98, 0.5), (45, 97, 0.75)]
)
@pytest.mark.parametrize(
    "day", [datetime(2026, 3, 28, 10, tzinfo=UTC), datetime(2026, 10, 24, 10, tzinfo=UTC)]
)
def test_clock_hour_switch_preserves_exact_48_hours_for_every_quarter(
    minute, fine_count, tail, day
):
    """Extend quarters to the next clock hour after 24h. Keep contiguous 48h coverage through both DST transitions.

    Starts at :15/:30/:45 need 99/98/97 fine steps. Coarse decisions start
    at :00 and only the final horizon edge may be shorter than an hour.
    """
    start = day.replace(minute=minute)
    grid = planning_grid(start)
    assert len(grid) == fine_count + 24
    assert all(duration == 0.25 for _, duration in grid[:fine_count])
    assert grid[fine_count][0].minute == 0
    assert grid[fine_count][0] - start == timedelta(minutes=15 * fine_count)
    assert all(begin.minute == 0 and duration == 1 for begin, duration in grid[fine_count:-1])
    assert grid[-1][1] == tail
    assert grid[-1][0] + timedelta(hours=tail) == start + timedelta(hours=48)
    assert sum(duration for _, duration in grid) == 48
    assert all(
        left + timedelta(hours=duration) == right for (left, duration), (right, _) in pairwise(grid)
    )
    assert demand_horizon(start) == solar_horizon(start) == grid
