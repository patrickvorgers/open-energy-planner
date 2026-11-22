# Understand savings and costs

Planning predicts an outcome. **Energy > Savings** compares recorded electricity cash cost with the same measured home and solar without battery exchange. Read the comparison basis and coverage before interpreting a monetary total.

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

**Energy > Savings** shows one comparison: the electricity cost with your battery
versus the same measured home and solar without a battery.

```text
battery saving = cost without battery - cost with battery
electricity cost = import cost - export income - applicable bonus income
```

For example, EUR 34.80 without the battery minus EUR 22.40 with the battery gives
EUR 12.40 saved. Negative savings mean that the battery increased electricity
costs in the selected period. This is a cash-cost comparison. It excludes battery
wear, investment costs and changes in the value of stored energy. Charging is
paid for when electricity is bought. Using previously stored energy can therefore
shift the apparent saving between days or months.

The without-battery amount is reconstructed from recorded AC energy flows.
Battery charging is removed from measured demand and battery discharge is added
back. The calculation keeps the measured solar production and the actual timing
of appliances, heating and EV charging. It does not run another battery schedule
or depend on a simulated state of charge.

Quarter-hour totals cannot reveal the exact sequence of flows within a quarter.
When no battery energy is removed, recorded gross import and export remain
unchanged. Otherwise, the reconstruction first offsets the opposite grid flow
and then increases import or export. Solar curtailment, hybrid clipping and the
timing of flexible loads are not independently reversed. The result measures the
battery's cash contribution, including how it was operated. It does not prove
that every saving was caused by the planner.

Choose the period through the calendar control. The arrows move to the previous
or next available period. The calendar includes standard periods and **Custom
period**, where you can choose both dates. The amount, both costs and the daily
chart always use the same selection. **With your battery** appears first in the
cost comparison. Open **Savings breakdown** to compare **Import cost**, **Export
income**, **Bonus income** and **Net electricity cost** for both sides. Income is
subtracted from import cost. The saving is the without-battery net cost minus the
with-battery net cost. Displayed amounts have two decimal places, so small rounding
differences are possible.

The current estimate uses the applicable bonus rate, price conditions and
eligible hours for both columns. It ignores the maximum export volume eligible
for bonuses. The estimated bonus can exceed the supplier's payment when the
contract's cap is reached.

The breakdown uses the same accepted quarters as the net costs. Opening it
reads the stored estimate for the selected period.

Open **How is this calculated?** for the basis and limitations. Values above the
bars and dates below them follow the selected daily or monthly bins. Labels are
reduced when space is limited. The chart retains negative values and gaps.
Keyboard users can focus it and inspect every amount and full date with Left,
Right, Home and End.

Hover over or tap a bar to see its **Savings breakdown**, with import cost,
export income, bonus income and net cost for both sides, followed by the saving.
The tooltip describes that day, or that month when the chart groups months.
Keyboard navigation opens the same details. Press Escape to dismiss them.
Unavailable component details stay unavailable even when the net saving is known.

The shared date control uses the Home Assistant installation timezone and the
signed-in user's date format. Forecast quality, Activity and Planning performance
use its single-day mode. Their charts and filters retain their existing meaning.
Planning performance permits only dates within its backend-reported retention
window. Disabled dates cannot be selected through arrows, shortcuts or the
calendar. Planning's relative future-horizon controls remain separate.

## Inspect coverage before totals

Both displayed costs include only intervals for which the same comparison can
be calculated. Missing intervals are never counted as zero and partial totals
are never extrapolated. A missing quarter does not stop later valid quarters.
There is no continuous simulated battery stock that a gap could invalidate.

Recorded AC meters, published prices and applicable contract rules are still
necessary. A DC solar meter behind a hybrid inverter cannot establish the
measured AC alternative. Such intervals remain unavailable for the comparison
instead of using a simulated conversion. Each quarter with valid measurements
and tariffs can be priced independently of prior bonus usage.

Savings states whether the selected period is complete or partial. It also
identifies reconstructed measurements. Measured zero savings remain zero. No
comparable evidence produces **Not available**, never a fictional EUR 0.00.
Connection failures clear the old amounts until the selected period is loaded
successfully. Configuration problems retain their specific recovery guidance.

## Provisional and finalized days

Only completed Home Assistant local days are evaluated. After midnight, yesterday
becomes provisional. Three complete local days remain available for missing
measurements, published prices and corrections. Monday closes at Friday 00:00.
Summer and winter clock changes do not shorten that recovery window.

Savings displays estimates calculated on the current basis. Finalized results
from older calculations remain archived outside that comparison. Provisional
days with the required quarter measurements are evaluated on the current
basis. Expired detail cannot be reconstructed from an old total.

Changing the installation timezone after daily history exists pauses processing
until the original Home Assistant timezone is restored. Missing Contract or
Installation location settings also pause processing with recovery guidance.
Existing results remain readable during a pause. Refreshing does not manufacture
missing measurements.

Planning continues to show expected economic costs, including its separate wear
and stored-energy valuation. Those future estimates have a different basis from
the retrospective electricity cash comparison. Do not add overlapping plans or
combine their predictions with measured Savings.

For the accounting and archive contracts, see [settlement and measured
savings](../development/SAVINGS.md).

**Next:** [Inspect forecast quality](forecasts.md#read-forecast-quality), or
[preserve your data](data-storage.md).
