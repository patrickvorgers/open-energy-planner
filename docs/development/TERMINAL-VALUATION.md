# Automatic battery inventory valuation

## Purpose and ownership

HORIZON-07/09 require stored energy to retain an estimated value beyond the
command horizon. An endpoint alone must not make usable energy worthless.
This is an app calculation. The HA integration, user automations and physical
reserve/safety responsibilities do not change. No new wizard field is required.

The configured installation uses **one estimated net price and one useful-volume
cap per battery**, not a detailed optimized continuation or a fixed final SoC.
The method is `bounded-average-v1`. Its purpose is an understandable boundary
estimate, not a claim to know prices after the forecast ends.

## Calculation

For a battery with final internal energy `E`, hard reserve `R`, useful internal
volume `U` and estimated net value `p`:

```text
estimated end value = min(max(E - R, 0), U) * p
economic objective = grid imports - grid exports - bonuses + wear - estimated end value
```

Grid amounts, wear and estimated terminal inventory remain separate in
`independent_costs`. A negative objective is not a promise of cash profit: it can
include the remaining value of energy already present before planning. Future
savings evaluation must compare inventory changes, not sum overlapping terminal
values from successive plans.

For example, 5 kWh remaining, a 1 kWh reserve, a 3 kWh useful-volume cap and an
estimated net rate of EUR 0.18/internal-kWh produce an end value of EUR 0.54.
The inaccessible reserve and the additional 1 kWh receive no discharge credit
in this finite estimate. This does not declare them permanently worthless.

## Price and physical-information window

The [price adapter](PRICE-FORECAST-SELECTION.md) combines the selected overlap forecast with EpexPredictor prices through 72 hours. Provider selection stays outside this model. Valuation consumes only composed prices and effective contract evidence.

1. Keep the executable target horizon at 48 hours: the first 24 hours in quarters
   and the last 24 in hours, 120 aligned decisions. Contract boundaries may refine
   billing segments without creating extra control decisions.
2. Prefer the first contiguous **up to 24 hours after the accepted command
   horizon** from already cached, fresh normalized prices. Apply the effective
   contract import/export formulas for those delivery times. Stop at missing
   prices or missing/invalid later contract coverage. Source freshness is checked
   against calculation time, not the future delivery timestamp.
3. Without any usable post-horizon prices, reuse the last available **up to
   24 hours of effective prices within the plan** as a proxy. For a full 48-hour
   plan, this means reusing hours 24–48 as an assumption about hours 48–72.
   It is not a third-day market forecast. A shortened horizon uses its available
   trailing window. No missing market price is silently replaced by zero.
4. The same trailing household/PV profile estimates later physical use. Where
   later prices are available, repeat that profile cyclically and split at price
   boundaries. This estimates physical quantities. It does not claim a weather or
   household forecast beyond the normal forecast horizon.

`price_basis` is `post_horizon_prices` or `trailing_window_proxy`. The latter's
window timestamps identify the original reused window. With real later prices,
the timestamps identify their actual future coverage. A partial later-price
prefix uses only that prefix. It is not padded with fabricated forecast hours.

The current Energy Price Forecast EU adapter still requests 48 hours without a
key. The price increment also fetches EpexPredictor hourly to extend cached prices through 72 hours. No paid service or key is introduced. The valuation calculation consumes the composed cache without fetching or choosing providers, and never extends the executable plan.

## One understandable average, with physical bounds

For each window segment:

- Estimate ordinary available PV AC production at each inverter, limited by its
  AC rating. Reserve that output before assigning battery discharge headroom.
- Limit battery AC output by its terminal discharge rating and DC/inverter
  efficiency. Batteries sharing an inverter divide its remaining headroom in
  proportion to these ratings. The same capacity is never promised twice.
- For a positive household deficit, value only avoided imports, bounded by that
  deficit and the connection import rating. Otherwise value only additional
  exports, bounded by remaining connection export headroom after ordinary PV.
- Divide the shared connection opportunity proportionally between the remaining
  battery AC ratings. This allocation is fixed for the estimate. Unused shares
  are not reassigned based on a candidate's final SoC.
- Convert the allocated AC amount to internal kWh. Its net rate is:

  ```text
  effective price * cell discharge efficiency * DC efficiency * inverter efficiency - cycle fee / capacity
  ```

- Retain only positive-net-value opportunities. There is no obligation to
  discharge at a loss in this continuation estimate.

The single rate `p` is the **internal-volume-weighted average** of those positive
net opportunities. It is not the maximum price, an unweighted segment average,
the historical acquisition cost or a trained forecast. `U` is their total
internal volume, capped at battery capacity minus reserve. No opportunities
means zero rate and zero useful volume, with explicit method evidence.

Independent arithmetic example: 0.25 internal kWh at EUR 0.40 and 1 internal
kWh at EUR 0.20 give `(0.25 * 0.40 + 1 * 0.20) / 1.25 = EUR 0.24/internal-kWh`.
Splitting the hour into four identical billing quarters does not change that
average. Cell/DC/converter losses reduce both delivered energy and its value.
Future discharge wear is deducted once. The earlier charging loss is already
reflected in actual stored energy and is not deducted again.

## Deliberate limits

This is a conservative capacity allocation and a simple average estimate, not
the optimal value of an entire future day. It excludes future recharging,
battery-to-battery transfers, speculative future bonus revenue and extra export
after fully serving a deficit within the same segment. It assumes ordinary PV
production rather than hypothetical future curtailment. Averaging deliberately
does not assign the highest-price opportunities to the first stored kWh.

These choices avoid an extra continuation solver, price-model training or a
mandatory final-SoC setting. They can understate useful opportunities. Repeating
a price/production profile can also overstate or understate actual future value,
especially around weather changes, weekends, contract boundaries and unusual
price events. The estimate is not a guaranteed lower bound on real future value.
Historical acquisition cost remains a separate accounting concern.

Replacing this method requires measured decision-quality evidence against this
simple baseline. Passing arithmetic scenarios does not prove forecasting skill
or lower real energy bills.

## Solver, evidence and independent checks

`ContractBasisSource` attaches immutable typed valuations after effective tariff
composition and horizon shortening. Each `InstallationProblem` requires exact
battery identities, matching reserves and feasible useful-volume caps. The input
snapshot/hash/archive includes the method, window, rate, cap and selected future
tariff evidence. Proxy tariff provenance remains in the original plan's normal
tariff evidence. Original archived plans are never rewritten.

HiGHS adds one continuous useful-inventory variable per battery, bounded by `U`
and final energy above reserve. It subtracts that variable times `p` from the
objective. No extra binary decisions, solver, endpoint or device command is added.
Extraction completes this independent continuous credit for a time-limited
feasible incumbent. It cannot alter physical flows or cash. Independent numerical
validation recomputes the capped formula from the final battery states, without
using native expressions. Fabricated financial totals are rejected.

`tests/app/installation/test_terminal_valuation.py` covers hand-calculated average rates, losses,
wear, reserves, caps, shared inverters/grid, PV headroom, absent batteries,
negative values, 120 mixed-resolution steps, shortened coverage, later-price
provenance, expired extensions, missing future contract coverage, profitable
retention/sales and settlement of multiple independently specified valid choices.
The production HTTP app tests verify per-battery evidence and aggregate inventory
totals in published plans. Existing solver regression expectations stay unchanged.

## Design rationale and alternative approaches

These are comparisons, not copied implementations or tests:

| Project | Observed approach | Difference from OEP |
|---|---|---|
| [DAO, revision 5443930](https://github.com/corneel27/day-ahead/blob/54439303304680469ae9ddd6a836bfbc6045a55c/dao/prog/day_ahead.py#L3353) | Values the initial/final inventory difference using a mean price and battery/converter discharge efficiencies. The non-netting branch uses the mean export price. | OEP uses a bounded useful volume and one automatically derived use-weighted rate. It does not copy DAO's netting functionality. |
| [EMHASS, revision 82c380c](https://github.com/davidusb-geek/emhass/blob/82c380cef56ee6f039b6f0ca37b5a57125721abf/src/emhass/optimization.py#L7294) | Uses an end-SoC target with deviation penalties. A positive optional reward factor values excess terminal energy using the final import-price slot. | OEP does not require a final-SoC target and allows economically preferable current sales. |
| [HAEO battery documentation](https://haeo.io/latest/user-guide/elements/battery/#salvage-value) | Exposes a configured salvage value per final stored kWh, default zero. | OEP derives its estimate automatically instead of asking the user to maintain a rate. |

The simpler published approaches influenced OEP's choice of one bounded average
instead of an hour-ranked marginal-value curve. No superiority claim follows.

## Forecast-source comparison and unresolved accuracy

[Energy Price Forecast EU](https://energypriceforecast.eu/en/private-pro/) states
that free price access supplies at most 48 hours. Longer access up to 120 hours
requires a Pro key. The source's [quality report](https://energypriceforecast.eu/en/forecast-quality/)
compares previously fixed production forecasts against later published prices.

[EpexPredictor](https://github.com/b3nn0/EpexPredictor) supports NL, local hosting
and a public API, with a documented 120-hour request example. Its published
performance results use a historical weather backtest. The author explicitly
warns that actual weather forecast errors can worsen performance. Its public
service has no availability guarantee. Local deployment introduces model/data
maintenance and optional ENTSO-E access. The public API is now connected through its separate price adapter. No local model is hosted.

The two published accuracy tables use different periods, lead definitions and
weather information. They cannot establish which source is better for OEP or NL.
The price adapter now freezes comparable responses at the same receipt time and scores matched delivery hours per lead bucket. Epex has no exposed model issue time. Independently settled battery-decision benefit remains a separate unproven quality measure. Longer coverage alone is not proof of better decisions.
