# Automatic forecast model selection

Forecast quality selects suitable models from predictions preserved before the
measured period. Solar, household demand and individual consumers share local
calendar-day boundaries and matched hourly energy comparisons. A specialized
physical or device model can be a baseline. Machine learning is not inherently
preferred, and forecasting never grants permission to execute an appliance.

## Daily evidence

The Home Assistant timezone defines each day. Normal days contain 24 hours,
spring transitions can contain 23 and autumn transitions 25. Only complete UTC
Recorder hours inside the local day are comparable. Fractional-offset boundary
fragments remain unmeasured rather than being invented from an hourly total.
`core/calendar.py` owns calendar boundaries. The pure
`forecast_selection/calculation/accuracy.py` owner supplies matching hours and
mean absolute hourly energy error in kWh.

For demand, `forecast_selection/accuracy.py` refreshes a pending next-day
prediction with the latest available original forecasts before midnight. On the
first observation after midnight it freezes that pending capture. Restart can
freeze an already retained capture, but cannot reconstruct a missing one.
Subsequent plans do not revise the frozen predictions. Every capture retains its
forecast documents, issue times, configuration/training versions and digest.

All currently compared alternatives use the same complete measured days. Missing
hours are unavailable, not zeros. The latest three eligible days within a
30-day lookback provide evidence. Each day records hourly MAE and absolute daily
energy error. A challenger must improve hourly MAE on each day by more than both
5.0% and 0.001 kWh per hour. If multiple challengers qualify, the lowest summed
MAE wins. A tie with the baseline cannot qualify. Equally scoring eligible
challengers retain their comparison order, currently Ridge before weather.
Insufficient or unreadable evidence, no consistent improvement or unavailable
model inputs retain the baseline. These thresholds are
conservative product policies, not a statistical accuracy guarantee.

A later bad day, changed measurement boundary or unavailable current model can
return selection to the baseline. Corrupt evidence is preserved, reported as
unavailable and cannot promote a model. Selection does not depend on prices,
contract bonuses or a simulated financial saving.

## Models and measurement boundaries

Household and device predictions reuse the measured weekday/hour baseline,
NumPy Ridge and, where appropriate, the existing weather-sensitive CPU XGBoost
model. Models retain their own training and input-coverage requirements. No
unsupported device-specific physics is fabricated. Solar retains PVWatts,
its independent learned model and supported HA forecasts. Solar keeps its
existing physical eligibility, source preferences, gain thresholds and supported
historical forecast backtests. See [solar forecasting](SOLAR-FORECASTING.md).

The household model trains on total measured AC demand minus independently
measured ready consumers. Available components are composed exactly once.
A selected consumer without sufficient trustworthy history remains inside an
explicit remaining-household boundary. It is never assigned a zero forecast.
That incomplete separation cannot authorize scheduling the unavailable consumer.
Model admission and measurement separation are different decisions. A measured
weekday profile can supply a separated device before an ML model qualifies.
See [demand forecasting](DEMAND-FORECASTING.md).

## Controlled operation is not spontaneous behavior

A planner can intentionally change when an appliance runs. Published activation
history already records controlled consumer identities. Demand selection reuses
that committed history instead of keeping a second intervention database. Days
with enabled published control are conservatively omitted from that consumer's
spontaneous-use comparison. An unseparated whole-house comparison omits days with
any controlled consumer. A separated household base remains comparable because
those measured consumers were removed from its training and actuals.

Publication proves planned intervention, not successful execution. Missing or
unreadable control history withholds model promotion while the baseline remains
usable. Cycle-profile learning and task/attempt execution feedback remain
independent. A detected actual cycle can still teach duration and energy after
its start was shifted. It does not establish what the unshifted start would have
been. See [shiftable appliances](SHIFTABLE-APPLIANCES.md).

## Persistence and inspection

`accuracy-pending-<family>.json` and `accuracy-day-<family>-<date>.json` are keys
in the active database's demand namespace, not filesystem files. Frozen captures
are immutable. The family includes measurement configuration, component boundary,
model identities, timezone and selection policy. Old financial or rolling-window
weather evidence cannot grant admission to the new daily policy. Existing
configuration identities, source IDs and original plans are unchanged.

Forecast quality shows current model readiness and completed local-day
comparisons beside the frozen daily view. Planning shows the chosen forecasts
and concrete schedules. Savings remains responsible for retrospective measured
energy and financial accounting.

The former financial shadow comparison code and immutable archives remain
available for research and regression replay. Production demand selection no
longer starts those extra optimizer comparisons once daily selection or device
composition applies. The injected chronological scorer remains a research seam.
