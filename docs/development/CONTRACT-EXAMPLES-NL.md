# Dutch contract formulas and bonus building blocks

This catalogue explains how Dutch-style contract components map to the [implemented formula language](CONTRACT-PRICES.md). It separates runnable price arithmetic from the separate [implemented bonus rules and counters](BONUS-RULES.md). Documentation remains in English. The examples concern Netherlands contracts.

All amounts below are EUR/kWh unless explicitly stated otherwise. `market` is the bare wholesale price already converted from EUR/MWh by the source adapter. Use decimal points. Tax/fee figures in generic examples are illustrative inputs, not maintained Dutch tax rates or supplier quotes. Check the applicable contract and effective dates before applying real settings.

## Supported contract scope

Base import/export formulas and bonuses are separate components. The user explicitly removed permitted-energy-route selection from bonus configuration. Configured bonus rules use total grid export. They do not distinguish direct solar export from battery export. Price/time conditions and chronological period counters remain required. This is a product simplification, not a change to the published supplier terms described below. Source-dependent exclusions therefore remain outside exact supplier coverage. Physical routing for energy balances and losses is unchanged.

## What can be configured now?

| Building block | Available input/syntax | Example | Status |
|---|---|---|---|
| Wholesale price | `market` | `market` | Implemented |
| Fixed per-kWh surcharge or deduction | Numeric literal and `+` / `-` | `market + 0.02` | Implemented |
| Percentage or tax multiplier | `*`, parentheses | `(market + 0.02 + 0.10) * 1.21` | Implemented. Constants are user-entered |
| Unit/rate arithmetic | `/` | `market / 2` | Implemented. `market` itself is already per kWh |
| Separate import/export terms | `import_formula`, `export_formula` | Import includes tax. Export has a different deduction | Implemented |
| Nonnegative price floor | `max` | `max(market, 0)` | Implemented. Only when the contract actually guarantees that floor |
| Price ceiling or bounded rate | `min`, nested `max` | `min(max(market, 0), 0.30)` | Implemented price bound, not a kWh or monetary-period cap |
| Positive-price percentage component | `max` and arithmetic | `market + 0.5 * max(market, 0)` | Arithmetic implemented. Eligibility is separate |
| Absolute value / unary signs | `abs`, unary `+` / `-` | `abs(market)` | Implemented. Not a default bonus treatment |
| Dated rate or tax changes | Ordered half-open `effective_from` / `effective_until` periods | Separate rules before/after a specified instant | Implemented. Aware timestamps required |
| Repeated time window / daylight | Local-time or sunrise/sunset condition | Daytime-only bonus | Implemented as separate bonus window |
| Chronological energy cap | Counter, period definition and initial measured balance | First aggregate export volume within a contractual year | Implemented with confirmed counter seed |

Only `market` is a formula variable. Names such as `tax`, `vat`, `hour`, `pv_export`, `soc` or `remaining_bonus` are not accepted identifiers. Substitute confirmed numeric values into arithmetic expressions. Do not put pseudocode into the API. Operators/functions and their limits are specified in [the grammar](CONTRACT-PRICES.md#supported-formula-language).

## Runnable generic recipes

| Recipe | Runnable formula | Result at market -0.10 | Result at 0 | Result at +0.10 |
|---|---|---:|---:|---:|
| Import with 0.02 fee, 0.10 tax, then illustrative 21% VAT | `(market + 0.02 + 0.10) * 1.21` | 0.0242 | 0.1452 | 0.2662 |
| Export at wholesale price | `market` | -0.10 | 0 | 0.10 |
| Export with 0.02 deduction | `market - 0.02` | -0.12 | -0.02 | 0.08 |
| Export with unconditional 0.02 addition | `market + 0.02` | -0.08 | 0.02 | 0.12 |
| Export guaranteed never to cost money | `max(market - 0.02, 0)` | 0 | 0 | 0.08 |
| Export with illustrative 0.30 rate ceiling | `min(market, 0.30)` | -0.10 | 0 | 0.10 |
| Constant import price | `0.25` | 0.25 | 0.25 | 0.25 |

A negative export result is a cost, not zero revenue. Do not use `abs(market)` to hide it. A price floor changes the financial terms and must not be added merely because negative prices are inconvenient.

For a dated tax change, create adjacent contract periods with different confirmed constants. For example, an illustrative import formula could change from `(market + 0.02 + 0.10) * 1.21` to `(market + 0.02 + 0.09) * 1.21` at `2027-01-01T00:00:00+01:00`. The timestamp and figures are an example, not a claim about enacted 2027 tax rates. The base model prices imported and exported energy separately.

## Reading the supplier examples

Each example first describes the public bonus, then maps it to OEP fields and
shows accepted bonus JSON. These are **configuration fragments**, not complete
API requests: put `export_formula` in the applicable base-price period and
`bonuses` at the contract root. Supply contract identity, import formula, base
period dates and `expected_version` as described in [contract settings](CONTRACT-PRICES.md#configuration-api).

The `2027-01-01` effective dates illustrate the user's contract configuration.
They are not claimed supplier launch dates. Use the customer's actual start date.
The two suppliers are alternatives, not two rewards to enable on one contract.

## Zonneplan Zonnebonus

### What the bonus means

Public explanation checked on **20 October 2026**: the base export price includes
0.02 EUR/kWh above the wholesale price. An additional 10% applies to that combined
positive price during daylight, with a 7,500-kWh calendar-year limit. Export from
a Zonneplan home battery is excluded. Source: [Zonneplan's explanation](https://www.zonneplan.nl/energie/zonnepanelen).

### Translation into OEP rules

| Component | OEP configuration |
|---|---|
| Base export price | `export_formula: market + 0.02` |
| Separate bonus rate | `rate_formula: 0.10 * (market + 0.02)` |
| Price condition | Enable: expression `market + 0.02`, comparison `greater_than`, threshold `"0"` |
| Daylight | `window: daylight`. Sunrise/sunset calculated from HA coordinates/timezone |
| Annual cap | `cap_kwh: 7500` |
| Calendar-year reset | January 1 in `Europe/Amsterdam` |
| Activation | Customer's actual `effective_from`. Stable `rule_id` across revisions |

```json
{
  "export_formula": "market + 0.02",
  "bonuses": [{
    "rule_id": "zonneplan-zonnebonus",
    "effective_from": "2027-01-01T00:00:00+01:00",
    "rate_formula": "0.10 * (market + 0.02)",
    "price_condition": {
      "expression": "market + 0.02",
      "operator": "greater_than",
      "threshold": "0"
    },
    "cap_kwh": 7500,
    "timezone": "Europe/Amsterdam",
    "reset_month": 1,
    "reset_day": 1,
    "window": "daylight"
  }]
}
```

The example uses the explicit price condition so eligibility remains separate from the rate. At a nonpositive combined price, the condition returns zero before evaluating the rate formula. Otherwise, `BonusRule.extra_rate()` validates the evaluated rate with `nonnegative()`. This validator rejects a negative rate as invalid. It does not clamp that rate to zero. Omitting the condition from the unbounded 10% formula would therefore fail validation at a combined price below zero. The separate bonus rule also applies the daylight window and annual cap. A per-kWh export formula alone cannot express those conditions, so it is not presented as an alternative to the complete bonus rule.

### Worked calculation

Assume one exported kWh during daylight and sufficient remaining quota:

| Market | Base income | Separate bonus income | Total income |
|---:|---:|---:|---:|
| -0.03 | -0.01 | 0 | -0.01 |
| -0.01 | 0.01 | 0.001 | 0.011 |
| 0 | 0.02 | 0.002 | 0.022 |
| 0.06 | 0.08 | 0.008 | 0.088 |

At night or after the cap, that last row pays only the base 0.08. With only
0.2 kWh quota remaining, the same one-kWh export earns `0.2 * 0.008 = 0.0016`
bonus, plus 0.08 base income. Displaying 0.09 would hide rounding. The unrounded
calculation is 0.088.

### Exactness boundary

OEP counts **all grid export**, including battery export and exports outside the
payment window. It cannot enforce the stated Zonneplan-battery exclusion. The
public summary also does not establish which excluded volumes advance the
supplier's annual counter. Confirm signed terms and the tax/VAT basis before
using the example for actual settlement. This fragment expresses the supported
rate/window/cap structure. It is not an exact supplier preset when those
attribution/counter differences apply.

## NextEnergy Zonnebonus

### What the bonus means

Public explanation and FAQ checked on **24 October 2026**: the bonus adds 50% of a positive
wholesale price for export between 06:00 and 22:00, up to 6,000 kWh per contract year.
Stored energy subsequently exported from a home battery is excluded. Existing
customers must activate the applicable bonus contract. Source:
[NextEnergy's explanation](https://www.nextenergy.nl/zonnebonus).

### Translation into OEP rules

| Component | OEP configuration |
|---|---|
| Base export price | `export_formula: market` in this illustration. Use the actual signed base tariff |
| Separate bonus rate | `rate_formula: 0.50 * market` |
| Positive wholesale price | `price_condition`: expression `market`, operator `greater_than`, threshold `"0"` |
| Local payment window | `window: clock`, `start_time: 06:00`, `end_time: 22:00` |
| Annual cap | `cap_kwh: 6000` |
| Reset anchor | Contract anniversary: use that price rule's Contract starts date |
| Activation | Customer's actual bonus-contract `effective_from` |

**Period assumption in the JSON below:** the illustrated contract starts on January 1. The [NextEnergy FAQ](https://www.nextenergy.nl/veelgestelde-vragen/ik-ben-al-klant-bij-nextenergy-hoe-krijg-ik-de-zonnebonus) explicitly says per contract year. The UI derives the anniversary from that price rule's Contract starts date. Replace the example dates with the actual customer's start. OEP uses local time with an inclusive start and exclusive end. The
supplier's precise endpoint and timezone semantics also require confirmation.

```json
{
  "export_formula": "market",
  "bonuses": [{
    "rule_id": "nextenergy-zonnebonus",
    "effective_from": "2027-01-01T00:00:00+01:00",
    "rate_formula": "0.50 * market",
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

### Worked calculation

Assume one exported kWh inside the configured window and sufficient quota:

| Market | Base income | Separate bonus income | Total income |
|---:|---:|---:|---:|
| -0.03 | -0.03 | 0 | -0.03 |
| 0 | 0 | 0 | 0 |
| 0.06 | 0.06 | 0.03 | 0.09 |

Outside the window or after the cap, the last row earns 0.06. With only 0.2 kWh
quota remaining it earns `0.06 + 0.2 * 0.03 = 0.066`. Multiplying every market
price by 1.5 would wrongly increase the negative-price charge.

### Exactness boundary

OEP does not distinguish stored exports from direct solar exports, so it cannot
enforce NextEnergy's battery exclusion. Its all-export counter is also not a
verified interpretation of the supplier's cap. Confirm the signed period,
counter and tax/VAT terms before actual use. The fragment does not claim exact
supplier settlement where those conditions differ. Source:
[NextEnergy terms index](https://www.nextenergy.nl/meer-info/voorwaarden).

## Starting counters and later rule changes

Both examples require a confirmed seed for the corresponding `rule_id` and active
period, tied to the same cumulative HA grid-export sensor. If period exports were
1,200 kWh when that meter read 4,500 kWh, a current reading of 4,700 means 1,400
kWh has been counted. Candidate plans never update that actual counter. See the
[counter request example](BONUS-RULES.md#actual-counters-versus-candidate-plans).
The normal HA history adapter automatically derives missing starting volumes by summing all configured grid-export sensors when coverage is complete. Only missing/incomplete evidence needs a confirmed starting volume.

For changed bonus terms, add a newer version with the **same** `rule_id` and the
new `effective_from`, rate and/or cap. Keep the period definition unchanged.
The newest applicable version wins without resetting consumed quota. Earlier
plans retain their original rules and expectations.

## How the bonus blocks combine

The following is a synthetic numerical example, not supplier wording. Suppose a bonus counter has a 100-kWh limit, starts at 98 kWh, and the next eligible aggregate grid export is 5 kWh. In the implemented aggregate-export model, only 2 kWh receive the extra rate. The remaining 3 kWh retain base compensation. At an extra rate of 0.03, bonus revenue is 0.06. `min(market, 100)` cannot express this: a price and an energy cap have different units.

Counter advancement is separate from the payment gate: all aggregate grid export advances the counter. A supplier whose terms count only selected volumes is not represented exactly by this model. Process volumes in chronological order. The solver cannot reserve remaining bonus quota for a later, more lucrative interval when the contract credits earlier exports first. Each reference/optimized scenario has its own evolving counter after an equal, historically determined starting balance. Replanning does not consume the actual measured counter.

| Separate block | Required meaning | Why a scalar price formula is insufficient |
|---|---|---|
| Eligibility conditions | Price sign, active contract and time/daylight | `market` alone has no clock |
| Period | Explicit calendar/contract year, timezone and reset instant | Dated rate periods do not maintain counters |
| Counter seed | Automatically derived from complete HA history, or explicitly confirmed when evidence is missing or incomplete | Starting every candidate with an unused budget overstates benefit |
| Chronological cap | Split interval energy when remaining eligible volume runs out | A global free allocation to any profitable interval changes contract meaning |
| Independent settlement evaluation | Recompute rates, eligibility, credited volume and revenue from plain flows | A native solver expression is not an independent financial check |

These are implemented generic building blocks under PRICE-06–11. See the [accepted JSON schema](BONUS-RULES.md#configuration). Supported supplier terms should populate those blocks. Provider-specific branches must not leak into the planner. Source-dependent export exclusions are not among those configurable blocks after the scope decision. Expose unsupported supplier exclusions explicitly rather than claiming a complete supplier preset.


The formula kernels above combine base and extra amounts only to explain arithmetic. In production configuration the base export rate and the extra bonus rate remain separate. Display and independently evaluate base export income and bonus income separately. The simplified total-grid-export assumption must not be hidden when comparing results with a supplier settlement that excludes selected sources.


Chronological counters enforce bonus budgets and caps. Dated base-price and bonus-rule versions support contract changes.
