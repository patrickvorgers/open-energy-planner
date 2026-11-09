# Home Assistant history adapter

The integration reads HA-owned hourly long-term energy statistics through the
in-process Recorder API. It does not open a database connection, execute SQL,
read Recorder files or obtain another HA token. Recorder remains optional:
`after_dependencies` orders setup when present without enabling it for users
who have chosen not to use it. The implementation targets pinned HA 2026.10.2.
The real Recorder API is part of the integration acceptance suite.

## Responsibilities and exchange

1. The app selects missing current-period bonus baselines from its contract and
   source configuration. Snapshot and observation responses include
   `bonus_history.request`, or `null` when no read is needed. The request binds
   contract/source/counter versions, export entity, annual boundaries and range.
2. The integration's `HistoryBridge` calls Recorder `get_metadata` and
   `statistics_during_period` through Recorder's executor. Only a `has_sum` energy
   statistic in Wh, kWh or MWh is supported. HA converts `sum` and `state` to kWh.
   Reads have a 15-second wait limit. Command-boundary timers remain separate.
3. The integration forwards compact `[start_epoch, sum_kwh, state_kwh]` rows to
   authenticated `POST /v1/history`. Provider metadata stays inside the adapter.
   The app parses immutable `EnergyHour`/`EnergyHistory` contracts, independently
   validates coverage and calculates the baseline.
4. The app serializes persistence with contract/source/manual-counter updates,
   archives the immutable counter book, then refreshes planning. A stale request
   returns 409. A manual baseline saved during a read cannot be overwritten.
   Existing matching seeds need no reread on replanning or app restart.
   When a new period/rule needs initialization, only current configured rule/period
   seeds remain in the new active book. Previous books and their evidence stay
   archived unchanged, so obsolete seeds cannot fill the 64-entry active-book limit.

`EnergyHistory` contains general energy statistics without bonus or supplier
fields. `BonusHistory` owns the financial use case. This is a reusable boundary
for later consumption forecasting, not an implemented predictor or a generic
import of every HA sensor.

## Independent calculation

For period start `T`, the anchor is the row for `[T - 1 hour, T)`. For the final
accepted completed row ending at `E`:

```text
seed.used_kwh = last.sum_kwh - anchor.sum_kwh
seed.meter_kwh = last.state_kwh
seed.observed_at = E
current_used_kwh = seed.used_kwh + current_export_meter_kwh - seed.meter_kwh
```

The requested end is the current UTC hour boundary minus one hour, allowing time
for HA statistics compilation. A fresh cumulative meter bridges the recent tail.
Historical meter resets are represented by HA's normalized `sum`. A reset after
the seed still requires reconciliation under the existing current-meter rules.
Both optimized and reference scenarios must start from the same actual counter.
No planned export updates the confirmed baseline.

Every hour from the anchor through `E` must be present, ordered and unique.
Missing/null/nonfinite values, decreasing sums, unsupported units and partial
coverage are rejected. Zero is accepted only as a numeric measurement. Local
contract dates become absolute UTC boundaries, so DST does not duplicate or
lose an hour. Non-hour-aligned boundaries cannot be reconstructed from hourly
statistics. Early in a new period the adapter waits for a completed-hour seed.
A confirmed manual baseline remains available.

This reproduces HA's hourly statistics and sampling semantics. It cannot prove
that the physical meter reported continuously or recover an exact boundary
reading where HA did not sample. It does not replace the separate settlement
measurement archive required for exact financial evaluation.

## Failure, limits and retention

- Failed reads retry after 15 minutes for the same request identity. Changed
  contract/source/counter/range identities may be tried immediately.
- Missing Recorder, unsupported statistics or rejected/incomplete history remain
  visible in `bonus_history.issue`. HA logs and maintains one actionable
  notification directing the user to repair the source or confirm a manual seed.
  A matching confirmed seed clears this condition. Unusable required current
  measurement sources take notification priority over missing history. After
  their repair, an unresolved history issue becomes visible again.
- An unreadable saved counter pointer/archive blocks automatic history requests.
  `bonus_counter_cache_unreadable` stays visible until explicit backup recovery
  or manual confirmation. Automatic initialization must never replace an unreadable
  confirmed baseline or clear its diagnostic as though no prior evidence existed.
- Only this route allows a 1 MiB body, bounded to 9000 hourly rows and an annual
  read window. Other routes retain 256 KiB. No endpoint accepts credentials,
  database locations or executable history expressions.
- The app retains used volume, physical meter, entity, contractual period,
  observation time, recorded time and immutable counter version. It does not
  copy the raw annual sensor history into its archive. Original plans retain
  counter-version and measured-input evidence. Include `bonus-counters/` in the
  consistent app backup.
- Automatic post-seed reset recovery, interpolation, the complete onboarding
  wizard, forecasting training and historical repricing remain separate work.

## Verification

`tests/app/history/test_history.py` checks independent arithmetic, gaps, invalid values,
historical resets, manual priority and annual boundaries across DST.
`tests/app/history/test_history_app.py` exercises authenticated HTTP, an annual transfer
larger than 256 KiB, immutable storage, fallback, delayed reports and the actual
starting budget passed to a solved plan.

`tests/custom_component/integration/history/test_recorder.py` imports fixtures only into isolated HA's temporary
Recorder database, then reads through the actual API and integration. It tests
Wh/kWh/MWh conversion, absent Recorder, missing metadata, incomplete coverage,
retry delay and notification recovery after manual confirmation. No production
HA data or physical devices are accessed.


## Demand and consumer training history

`demand.request` uses `energy_history_range` for monthly availability discovery
and `energy_history_chunk` for bounded annual cumulative-energy transfers.
Responses echo the exact basis and request version. Both operations require one
sensor and its Recorder `metadata_id`, `source` and original energy `unit`.
The app accepts only the current pending request. Request size stays bounded
at 1 MiB and each hourly response stays below 9000 rows and 367 days.

The bridge reuses Solar's existing monthly discovery and annual capture path.
It preserves raw sum/state evidence and normalizes requested energy to kWh.
The shared app importer trains from cumulative sums. Physical meter baselines
for financial settlement retain their existing independent validation.

Available history is not limited to the most recent year. Closed annual chunks,
empty-year coverage and checkpoints survive restart in the active database.
A failed sensor backs off for fifteen minutes while other meters proceed.
Household channels activate together. Consumers activate independently after
completing their currently requested years. Invalid intervals are omitted
without globally blocking otherwise sufficient training data. See
[household demand forecasting](DEMAND-FORECASTING.md#full-available-history).
