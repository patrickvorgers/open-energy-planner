# Price timeline and control grid

## Ownership

Provider adapters retain original price intervals, issue/receipt metadata, units,
source versions and native resolution. They do not apply the planner's coarser
control policy. The combined price timeline selects published prices first,
then the preferred usable forecast for each lead bucket, with fallback to
another usable source. It projects these selected rates onto UTC quarters.

An hourly rate becomes four equal quarter rates. Its native resolution remains
60 minutes and its exact original source version remains unchanged. This is
repetition, not interpolation or a new quarter-hour prediction. Negative prices
remain valid. Missing prices remain missing. Coverage stops at the first gap.
Expired forecasts remain excluded. Historical source archives and saved plans
are not rewritten.

Rolling reads may start or end inside a quarter. These first/last pieces are
clipped to the requested interval. Genuine source boundaries remain explicit
rather than being averaged away. Forecast preference changes are evaluated at
quarter boundaries, avoiding a synthetic off-grid split from a rolling clock.
The standard aligned provider intervals produce only full quarters between the
clipped edges.

## Control grid

The normal forecast basis starts on the current UTC quarter. Its end remains
exactly 48 elapsed hours later. The fine section lasts at least 24 hours and
continues to the first whole UTC hour at or after that threshold. Coarse control
blocks begin on whole hours. The final block is clipped to the original horizon.
Explicit unaligned synthetic starts preserve their supplied instant and clip
transition/final edges without extending the horizon.

| UTC start | Fine section | Fine groups | Remaining groups | Final group |
| --- | --- | --- | --- | --- |
| 10:00 | 24h | 96 | 24 full hours | 1h |
| 10:15 | 24h45m | 99 | 23 full hours and one quarter | 15m |
| 10:30 | 24h30m | 98 | 23 full hours and one half hour | 30m |
| 10:45 | 24h15m | 97 | 23 full hours and three quarters | 45m |

Demand and solar forecasts use this same grid. UTC elapsed durations preserve
continuity across daylight-saving changes. Display times use Home Assistant's
timezone. A rolling planning read may clip the elapsed part of the first quarter.
The grid policy remains 48 hours from its basis start. Planning clips elapsed
initial time and uses only common available price, demand and solar coverage.
Cached forecasts can therefore shorten the operational plan. This existing
coverage fallback never extrapolates an extra tail or extends the 48-hour grid.

## Exact billing inside coarse controls

The contract basis retains quarter-hour tariff evidence and splits genuine
contract, bonus, reset and paid-window boundaries exactly. Every resulting
billing segment retains its original control group. Existing solver bindings
share the battery/converter power decision across that group. There are no
additional power decisions at internal price boundaries. Physical balances,
state transitions, price charges, eligible bonus energy and caps remain evaluated
on their exact segments. Forecast energy is proportionally allocated at its
original interval mean rate, preserving total kWh without inventing finer detail.

This is deliberately not an average hourly tariff. Four different quarter prices
may coexist under one hourly power decision. Bonus eligibility and import/export
constraints must not disappear through averaging. The number of billing rows is
not the number of independent control decisions. Original price lines and the
interval inspector retain exact stored boundaries. Balance bars alone project
the recorded flows onto fixed quarters using duration-weighted mean power.
Their tooltip identifies partial horizon coverage. This presentation does not
round accounting edges or change charges, eligible energy or volume caps.

## Verification

Regression tests cover every quarter-aligned start, both European DST changes,
exact 48h coverage, final partial hours, shared demand/solar grids, negative hourly
price repetition, raw provenance, published-price priority, missing coverage,
contract-year boundaries and 192 billing quarters with 123 controls for a :15 start.
Existing solver regressions retain independent financial and physical oracles.
