# Aggregate-export bonuses

Base import cost, base export revenue and bonus income are separate financial
components. Bonuses apply to total grid export, including battery export. A
contract requiring source-dependent exclusions is not represented exactly by this
model. The Dutch examples are not complete supplier presets.

## Modules and ownership

| Module | Responsibility |
|---|---|
| `contracts/bonus.py` | Immutable dated rules, annual periods and confirmed counter seeds |
| `contracts/windows.py` | Shared eligibility and exact UTC boundaries for Always, Clock, Daylight and Monthly schedule |
| `contracts/compilation.py` | Convert rules, accepted tariff segments and current meter evidence into plain solver inputs |
| `optimization/export_bonus.py` | Plain prepared contracts and numerical chronological settlement. No native solver |
| `optimization/adapters/highs/export_bonus.py` | Native exact min/max constraints and control-group equal-power constraints |
| `optimization/costs.py` and `validation.py` | Independently recompute income and reject inconsistent native credits/counters/costs |
| `storage/counters.py` | Versioned, atomic counter archives, separate from proposed plans |
| HA integration | Select/read cumulative grid-export sensor. Forward configured HA coordinates/timezone. Publish commands |

Provider payloads, Home Assistant objects, persistence and native expressions do
not cross into rule semantics. The app performs the calculations. Astral 3.2
(Apache-2.0) calculates solar events locally using the HA location. No weather
service or user-entered solar coordinates are required for bonus windows.

## Configuration

Add `bonuses` to the existing authenticated `PUT /v1/contracts` request:

```json
{
  "bonuses": [{
    "rule_id": "daytime-extra",
    "effective_from": "2027-01-01T00:00:00+01:00",
    "rate_formula": "0.5 * max(market, 0)",
    "price_condition": {"expression": "market", "operator": "greater_than", "threshold": "0"},
    "cap_kwh": 6000,
    "timezone": "Europe/Amsterdam",
    "reset_month": 1,
    "reset_day": 1,
    "window": "clock",
    "start_time": "06:00",
    "end_time": "22:00"
  }]
}
```

This is the bonus fragment of a synthetic contract, not a complete request or a
supplier preset. Include the existing contract identity, currency, market area,
base periods and `expected_version`. Formula syntax is the same bounded grammar
as base prices. An optional `price_condition` compares its expression with an explicit
decimal threshold in EUR/kWh. An omitted or null condition imposes no price gate. Extra rates must be nonnegative. `market` remains the only formula
variable. No executable script or source-route filter is accepted.

Window choices are `always`, `clock`, `daylight` or `monthly_schedule`. Clock windows require the two
HH:MM fields, support crossing midnight and use the contractual timezone.
Daylight uses the configured HA latitude, longitude and timezone. Other windows
require absent or null clock fields. All windows are half-open. Both repeated autumn clock
hours are considered. Nonexistent spring clock edges are skipped. The offset
transition itself splits billing when clock eligibility changes there. Missing HA
location or a polar date without a sunrise/sunset pair produces an explicit
unavailable-window reason, never an invented paid interval.

Monthly schedule uses a required `monthly_schedule` array of exactly twelve
January-to-December entries. Each is null (no payment) or an object containing
only `start_time` and `end_time` in HH:MM. Equal endpoints are rejected. An
all-null schedule pays nothing. For example, January 22:00-06:00 and all other
months disabled is:

```json
{"window": "monthly_schedule", "monthly_schedule": [
  {"start_time": "22:00", "end_time": "06:00"},
  null, null, null, null, null, null, null, null, null, null, null
]}
```

The opening local date selects the month. January's last night continues until
06:00 on 1 February even if February is disabled. December nights likewise cross
the year boundary. Overlapping tails and next-day ranges form one payment window
and never double the rate. Monthly schedule requires absent or null top-level
clock fields. Other strategies omit monthly entries. The Contract page uses one compact disclosure
with twelve enabled/start/end rows on desktop, tablet and mobile.
Switching strategies disables inactive controls and omits monthly entries when
another strategy is saved. It uses the
same authenticated preview/save flow and never calculates financial rates in
the browser. Monthly schedule does not change rates, annual caps, measured
counter provenance or physical control resolution.

Across all rule identities, the newest `effective_from` not after the
delivery instant is the only active bonus rule. Rules never stack. A future version can be configured in advance. Before the first
version its rate/cap are zero. Rate/cap changes do not reset volume. All versions
of an identity must keep the same contractual timezone and annual reset anchor.
Ambiguous equal dates are rejected. Different IDs retain separate counters and provenance, but never stack payments. Up to eight IDs and 64 versions are supported. Annual reset anchors
must exist every year. February 29 is rejected.

`POST /v1/contracts/preview` also returns `bonus_preview`: extra-rate samples at
negative, zero and positive market prices. The scope is explicitly
`rate_and_price_condition_only`. These samples do not pretend to evaluate time
windows, available quota or actual energy.

## Actual counters versus candidate plans

All exported kWh advance the period counter, including exports outside a payment
window or at a zero bonus rate. Quota is consumed chronologically, never freely
allocated to a more profitable later interval. Counters remain uncapped so later
cap increases retain previously exported volume. A new future contractual period
starts with zero projected exports. A new actual period requires a confirmed
baseline rather than silently assuming unused budget.

Select an energy sensor as `grid.export_energy` in HA's source options. It must
provide fresh monotonic cumulative export, normalized to kWh, and be marked
required for bonus planning. The [history adapter](HA-HISTORY.md) automatically
derives missing current-period baselines from complete HA hourly export statistics.
An explicit confirmed baseline remains the fallback, configured using
authenticated `PUT /v1/bonus-counters`:

```json
{
  "expected_version": null,
  "seeds": [{
    "rule_id": "daytime-extra",
    "period_start": "2027-01-01T00:00:00+01:00",
    "used_kwh": 1200,
    "meter_kwh": 4500,
    "entity_id": "sensor.grid_export_total",
    "observed_at": "2027-06-01T12:00:00+02:00"
  }]
}
```

The current volume is `used_kwh + current_meter_kwh - meter_kwh`. The two baseline
values must describe the same observation instant. Seeds are per rule/period,
bounded to 64 entries, with server-assigned recorded time and content version.
`GET /v1/bonus-counters` reads them. Stale writes return 409, malformed writes 400,
anonymous requests 401. Replanning never changes confirmed seeds.

Missing seed, mismatched source/period, stale reading or a reading below the seed
meter blocks new bonus planning. A resetting/daily sensor is not a valid lifetime
counter. A reset that has already grown past its baseline cannot be inferred from
a single sample. Source changes/resets require reconciling the confirmed seed.
Historical initialization requires complete hourly statistics including the
period-boundary anchor. It never fills gaps with zeros or overwrites a matching
seed. See the history adapter for sampling limits and recovery.

## Billing and control resolution

Refine intervals at price, contract, bonus-version, local-window and reset edges.
Keep exact rates and source provenance per billing segment. Forecast PV/load
energy is distributed using the declared average power within the original
forecast interval. Finer billing does not claim a finer measured forecast.

Equal terminal battery charge/discharge power is enforced across all billing
segments of one original control interval. Commands merge those segments back
into the same original decision. The 48-hour horizon therefore still has 96
quarters plus 24 hourly commands, even with more than 192 billing segments.
There is no averaged tariff and no extra within-step arbitrage. The current [installation planner](INSTALLATION-PLANNING.md) composes multiple PV sources, batteries and converters. Bonus billing stays at the aggregate grid boundary.

Per-plan input preserves rule and counter versions, meter evidence, the HA
location and per-segment rates/caps/periods. Output preserves per-rule quota and
counters plus separate total bonus income. Old plan bytes are not changed.
Back up the active SQL database together with the matching app data and HA integration configuration. Rule revisions, confirmed counters, source/model archives, plans and evaluations all belong to that database. `contracts` and `bonus-counters` are document namespaces, not filesystem directories. External PostgreSQL/MariaDB/MySQL backups require their server tools and are not automatically included in an HA backup. Follow the [consistent backup and recovery procedure](PLAN-ARCHIVE.md#backup-and-recovery), including SQLite WAL consistency.
Archive writes precede pointer publication. Cancellation drains the serialized
writer. Unreadable archives remain intact and are reported explicitly.

## Independent acceptance evidence

`tests/app/contract/test_export_bonuses.py` covers literal expected payouts,
initially exhausted quota, chronological allocation, zero-rate volume, cap
changes, resets, additive bonuses and battery/PV aggregation. It enumerates all
eight feasible schedules of a small on/off problem with rational prefix-volume
intersections, checks every schedule's settlement, then compares the native
optimum with that independently computed minimum. This oracle imports neither
the model builder nor the production settlement function to calculate expected
amounts. There is no claim of exhaustive coverage of continuous powers.

`tests/app/contract/test_contract_bonuses.py` covers time/price gates, DST, real local solar
events, dated versions, measured baselines, immutable persistence and exact
120-command projection. `tests/app/contract/test_bonus_app.py` exercises authenticated local
HTTP configuration through real solving/publication. The actual HA suite checks
native sensor selection, energy normalization and forwarding HA's location.
Deliberately forged credits, counters, identity, revenue and NaN must be rejected.

[Savings](SAVINGS.md) supplies historical repricing. [Solar](SOLAR-FORECASTING.md) and [demand](DEMAND-FORECASTING.md) forecasting provide production inputs. Durable state uses the [shared SQLAlchemy database](DATABASE-SCHEMA.md). The app-owned [Contract page](CONTRACT-PRICES.md#contract-page) edits these same rules and shows measured remaining budgets. It does not add a second calculation language.


A contract has at most one active bonus rule at any instant. Select the latest
rule whose effective start has been reached, across all bonus identities. A newer
rule supersedes the previous rule exactly at its start. Future rules do not apply
early and rules never stack. Preserve historical definitions, frozen plan inputs
and counter provenance. Reject ambiguous equal effective dates across identities.

## Explicit price comparisons

The immutable `ComparisonCondition` belongs to the arithmetic domain, independent
of bonuses, HA, HTTP and solver objects. Supported operators are `greater_than`,
`greater_than_or_equal`, `less_than`, `less_than_or_equal`, `equal` and `not_equal`.
The UI spells out these choices in sentence case. Enable price condition reveals
Price expression (default `market`), Comparison and Threshold (€/kWh, default `0`).
Disabled controls are hidden and submit null. The server alone evaluates formulas.

Formula arithmetic uses 28 significant Decimal digits with round-half-even.
Comparisons happen before float conversion, with no epsilon. Thresholds are
serialized as decimal strings to preserve their exact value. For example,
`market + 0.02` Less than or equal to `0` includes market exactly -0.02.
Eligibility never changes the rate formula, payment window or chronological cap.
Compilation and independent settlement both call the same validated domain rule.

The combined regression matrix in `tests/app/contract/test_contract_bonuses.py` checks clock
and daylight windows together with a strict positive combined-price condition
and remaining annual quota. It covers negative prices, the exact zero boundary,
a positive bonus at a negative market price, an open or closed window, partial
quota and exhausted or exceeded caps. At market 0.06 EUR/kWh, a 10% bonus on
`market + 0.02` pays 0.008 EUR for one eligible kWh, or 0.0032 EUR when only
0.4 kWh of quota remains. Solver output and separate settlement are checked
against those literal expectations. Export advances the counter even when a
price or time condition prevents payment.

The unreleased development format has no compatibility converter. If an old
contract archive is unreadable, stop active planning and resubmit the complete
contract through authenticated `PUT /v1/contracts` using the current format and
`expected_version: null` when no readable current contract exists. Preserve the
old archives and frozen plans. Do not clear the database or unrelated history.
Verify the new preview and readiness before enabling planning again.


## Central bonus budget

The Contract page has one **Bonus budget** panel for the active supplier contract.
It is independent of price rule editing. The opening is already used export since
the annual reset or contract start, as of its HA measurement date. It is not the
raw lifetime meter reading and it is not the remaining quota.

Total used = opening used volume + measured export since opening. The latter is
the sum of the physical grid export meter advances after the opening. All export
counts, including unpaid price/time windows. Per-member reset checks remain active.
The panel shows period start, maximum, opening, origin, measured advance, total,
remaining and updated-through date. Details show the selected meters and calculation.

Automatic openings show the complete hourly HA history period, boundary anchor,
statistics endpoint difference, row count and digest. Manual openings explicitly
identify the confirmed volume and the HA reading date. Legacy openings without
origin evidence display **Origin unknown** and retain their original values.

When history cannot establish a usable budget, **Confirm opening volume** records
one used-kWh value through the displayed current HA measurement date. Its matching
physical readings come from HA. A stale measurement/configuration fingerprint is
rejected without a write. Refresh the budget before confirming against newer data.
There is no arbitrary historical date picker because that would pair an old volume
with an unrelated current meter reading. A new price rule does not reset usage.
Annual resets and new supplier starts are automatically separate budget periods.
