# Understand savings and costs

Planning predicts an outcome. **Energy > Savings** evaluates retained metering and a separate simulated reference. Read the comparison basis and coverage before interpreting a monetary total.

## Expected benefit on Planning

Each scenario starts with **Expected economic cost**. Optimizer and battery self-consumption also show **Expected benefit versus without battery**:

```text
expected benefit = without-battery economic cost - scenario economic cost
```

For example, a synthetic baseline of EUR 14.21 and optimizer cost of EUR 9.25 imply EUR 4.96 expected benefit. If self-consumption costs EUR 12.00, its benefit against that same baseline is EUR 2.21. These are alternative scenarios. Do not add their benefits together.

These amounts follow the selected Planning chart period, including its change in stored-energy value. The caption states the covered period. **Full period** shows the available duration, normally 48 hours for a new plan. They are not measured savings or annual investment returns.

## Read the cost breakdown

In each Planning scenario, **Cost breakdown** lists **Import cost**, **Export income** and **Bonus income** separately. Use them to check the net amount:

```text
net electricity cost = import cost - export income - bonus income
economic cost = net electricity cost + battery wear - battery value change
```

For example, 20.00 EUR import cost minus 4.50 EUR export income minus 0.50 EUR bonus income gives 15.00 EUR net electricity cost. Negative income increases costs. An unavailable amount stays unavailable. Displayed amounts have two decimal places, while totals use full precision. Small rounding differences are possible when subtracting the displayed amounts. These figures cover the selected chart period.

Battery value change is the estimated end value minus the starting value of stored energy. A positive value change reduces economic cost. It is not money paid into your account. The planner does not treat the battery's remaining usable energy as worthless merely because the horizon ends.

Every view uses the same valuation basis frozen with the original plan. Only its opening and closing battery stocks change. Reserves and useful-energy limits still apply. Partial intervals use their original mean flows and interpolated battery endpoints. Bonus limits count earlier export chronologically, including export outside the visible window. Changing the view never re-optimizes the plan.

Open **Cost breakdown** to separate cash, wear and stored-energy value. Check effective tariffs and bonus eligibility before comparing the result with a supplier bill. Investment costs and unsupported billing arrangements are not implicitly included.

## Read evaluated Savings

Choose a period and inspect **Daily results**. The actual side uses recorded meters and published settlement prices. **Battery self-consumption** simulates storage without price optimization. **Without battery** simulates the same measured household demand and available solar without storage. Each scenario has its own coverage and bonus counters.

| Planning comparison | Evaluated Savings comparison |
|---|---|
| Predicts a future horizon | Evaluates completed local days from chronological quarter-hour measurements |
| Uses the selected original plan's forecasts and prices | Uses retained metering and published settlement prices |
| Shows both battery scenarios versus without battery | Preserves actual operation, continuous self-consumption and without-battery costs |
| Starts each future comparison from the recorded common state | Carries independent reference stock and bonus counters through time |

The reference is simulated, not an independently measured parallel household. A favourable difference does not prove that OEP alone caused it. Real equipment behavior, forecasts and the reference assumptions all matter.

## Inspect coverage before totals

Missing readings, a missing settlement price or an unestablished opening bonus counter can delay evaluation. Estimated hourly or boundary reconstruction remains labelled estimated. A gap is not a zero-cost interval and does not reset the reference to a fresh battery.

A contract may start after the earliest available Home Assistant history. For example, a June contract can use recorded statistics even when those statistics begin in January. Historical bonus evaluation keeps the contract start and annual reset separate. If a newer bonus checkpoint cannot be traced back through stored measurements, the app automatically requests a historical statistics endpoint using the same confirmed reference and any manual missing-period export. It then needs only the local measurements between that completed hour and the requested interval. Do not change a valid contract date or enter an estimated bonus total to bypass a wait.

Measured history normally becomes available after the completed interval and Recorder delay. Repeatedly refreshing does not create missing evidence. Open the relevant warning or use [troubleshooting](troubleshooting.md#savings-or-quality-is-incomplete).

### Provisional and finalized days

Savings initially selects yesterday in Home Assistant's timezone. Device labels
follow current Home Assistant Energy names while finalized calculations remain
unchanged. Changing the installation timezone after daily history exists pauses
automatic processing and retains the existing calendar evidence.

The current day is not evaluated yet. After local midnight, the previous day becomes provisional. Three complete local days remain available for missing measurements, published prices and corrections. For Monday, those are Tuesday, Wednesday and Thursday. Monday closes at Friday 00:00 in Home Assistant's timezone. Summer and winter clock changes do not shorten this calendar window.

Each scenario closes as **Complete**, **Partial** or **Unavailable**. Partial totals cover only the displayed measured time. They are never stretched to represent a full day. Actual costs can remain usable while a simulated scenario is unavailable. The benefit includes only intervals accepted for both scenarios being compared. A complete actual-cost total and a partial reference-cost total cannot simply be subtracted.

After finalization, the daily costs, energy totals, coverage, missing ranges, battery endpoints and calculation versions remain available. Later changes to terms or prices do not silently rewrite a finalized day. Detailed temporary quarters can then expire. **View retained day plan** opens the original daily plan when one was published that day. Its forecast is separate from the measured daily result.

On upgrade, existing history receives a recovery window before old days can close. Backfill proceeds in the background. An old day appearing first does not mean the entire historical import has finished.

A gap in physical measurements never resets a simulated battery to the actual battery. Unknown simulated bonus usage never becomes a fresh allowance. Later actual measurements and independently recoverable scenarios may still produce partial results. Missing prices alone do not erase known physical battery progress.

## Avoid misleading comparisons

- Do not sum benefits from overlapping future plans.
- Do not compare different time coverage as though it were the same period.
- Do not call stored-energy value cash profit.
- Do not substitute expected flows for measured execution.
- Do not treat lower grid import and lower cost as interchangeable objectives.

For implementation and exact measurement-quality rules, use [settlement and continuous reference](../development/SAVINGS.md) and [battery inventory valuation](../development/TERMINAL-VALUATION.md).

**Next:** [Inspect forecast quality](forecasts.md#read-forecast-quality), or [preserve your data](data-storage.md).
