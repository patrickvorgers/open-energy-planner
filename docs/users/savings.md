# Understand savings and costs

Planning predicts an outcome. **Energy > Savings** evaluates retained metering and a separate simulated reference. Read the comparison basis and coverage before interpreting a monetary total.

## Expected benefit on Planning

Each scenario starts with **Expected economic cost**. Optimizer and battery self-consumption also show **Expected benefit versus without battery**:

```text
expected benefit = without-battery economic cost - scenario economic cost
```

For example, a synthetic baseline of EUR 14.21 and optimizer cost of EUR 9.25 imply EUR 4.96 expected benefit. If self-consumption costs EUR 12.00, its benefit against that same baseline is EUR 2.21. These are alternative scenarios. Do not add their benefits together.

These amounts cover the whole selected original plan, including remaining stored-energy value. Zooming the chart does not change their period. They are not measured savings or annual investment returns.

## Read the cost breakdown

```text
net grid cash cost = import cost - export income - bonuses
economic cost = net grid cash cost + battery wear - battery value change
```

Battery value change is the estimated end value minus the starting value of stored energy. A positive value change reduces economic cost. It is not money paid into your account. The planner does not treat the battery's remaining usable energy as worthless merely because the horizon ends.

Open **Cost breakdown** to separate cash, wear and stored-energy value. Check effective tariffs and bonus eligibility before comparing the result with a supplier bill. Investment costs and unsupported billing arrangements are not implicitly included.

## Read evaluated Savings

Choose the evaluation version and period. The actual side uses retained measurements and published settlement prices. The reference simulates continuous battery self-consumption without price optimization.

| Planning comparison | Evaluated Savings comparison |
|---|---|
| Predicts a future horizon | Evaluates non-overlapping historical intervals |
| Uses the selected original plan's forecasts and prices | Uses retained metering and published settlement prices |
| Shows both battery scenarios versus without battery | Compares actual EMS operation with continuous self-consumption |
| Starts each future comparison from the recorded common state | Carries independent reference stock and bonus counters through time |

The reference is simulated, not an independently measured parallel household. A favourable difference does not prove that OEP alone caused it. Real equipment behavior, forecasts and the reference assumptions all matter.

## Inspect coverage before totals

Missing readings, a missing settlement price or an unestablished opening bonus counter can delay evaluation. Estimated hourly or boundary reconstruction remains labelled estimated. A gap is not a zero-cost interval and does not reset the reference to a fresh battery.

Measured history normally becomes available after the completed interval and Recorder delay. Repeatedly refreshing does not create missing evidence. Open the relevant warning or use [troubleshooting](troubleshooting.md#savings-or-quality-is-incomplete).

Corrections to published prices, rules or confirmed opening evidence can create a new evaluation version. Original plans and prior evaluation evidence remain distinct. A correction does not rerun an old plan as if its inputs had always been different.

## Avoid misleading comparisons

- Do not sum benefits from overlapping future plans.
- Do not compare different time coverage as though it were the same period.
- Do not call stored-energy value cash profit.
- Do not substitute expected flows for measured execution.
- Do not treat lower grid import and lower cost as interchangeable objectives.

For implementation and exact measurement-quality rules, use [settlement and continuous reference](../development/SAVINGS.md) and [battery inventory valuation](../development/TERMINAL-VALUATION.md).

**Next:** [Inspect forecast quality](forecasts.md#read-forecast-quality), or [preserve your data](data-storage.md).
