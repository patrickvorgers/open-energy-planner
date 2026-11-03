# Home Assistant measurement-source boundary

This boundary implements CONFIG-05-07 and ARCH-09-10. Measurement lookup and normalization run inside the HA integration. The app does not connect back to HA or receive an HA origin/API token.

## Responsibilities

- `custom_components/open_energy_planner/measurements.py` defines immutable source bindings, readings and issues. It imports no HA framework, HTTP, ORM or solver package. Bindings represent app-owned configuration. These values do not introduce a second configuration owner in HA.
- `measurement_sources.py` contains `HomeAssistantMeasurementSource` and `StateNormalizer`. The source reads only configured IDs through the in-process HA state machine. HA dictionaries stay inside this integration adapter.
- No REST source reader, independent HA credentials or extra app-to-HA connection remains. Selected observations travel through the same authenticated integration/app connection. The app wizard owns source selection. HA options contain only the app link. No HA API token or second configuration store is introduced.

## Measurements and validation

| Quantity | Accepted units | Normalized unit |
|---|---|---|
| Power | W, kW | W |
| Energy | Wh, kWh, MWh | kWh |
| Battery SoC | % | % |

Source selection must confirm power direction: grid import positive. Battery discharge positive. A binding multiplier of +1 or -1 translates the source sign. Energy and SoC cannot invert sign. Entity names do not determine relationships, capacity, limits, forecasts or direction. Energy counters are not instantaneous power. Measured power is not a forecast.

Only selected bindings are required. An installation without a battery has no implicit SoC source. Optional source faults remain visible without blocking batch readiness. Required faults make the batch unready. Zero remains valid. Missing, unknown, unavailable, non-finite, incompatible-unit, out-of-range SoC and negative cumulative-energy values are never replaced by synthetic data.

A binding may name a non-empty device sample timestamp attribute. Its age alone does not invalidate the current HA state. Otherwise the integration reads the current available HA state and preserves `last_reported`, falling back to `last_updated` when report metadata is absent. Neither original timestamp is an implicit heartbeat. A stable export counter, minimum SoC or zero power may remain unchanged for hours. UTC conversion, arithmetic and parsing are guarded. Overflow becomes an `invalid_timestamp` source issue. Complete integration captures normally expire after 300 seconds, independently of state age. Capture time identifies when the integration checked HA, not a new physical sample or proof of device connectivity. Forwarded reports retain original device/HA/state-time provenance. Unavailable, unknown, invalid and future-dated values remain unusable.

Each reading retains its own timestamp. A batch does not claim atomic physical sampling across devices. The snapshot assembler checks the current capture deadline and rejects unusable core inputs. EV SoC and thermal inputs follow the same distinction with their existing report limits. Synthetic and HA input modes remain explicitly separate.

## Validation

Unit scenarios independently cover conversions, signs, optional-device faults, immutable results, invalid timestamp configuration and UTC overflows. A real HA test reads a fixture sensor through `hass.states` without REST or hardware. Import tests reject relative, absolute and package-form imports of the adapter from measurement contracts.

## Observation-to-planning increment

Start the app with `--live` instead of `--demo`, using the existing installation token. Configure the connected HA Energy dashboard before opening the app's `/setup` wizard. The app reuses its complete household energy balance, production meters and HA time zone. It requests only missing instantaneous grid power and paired battery SoC/actual-power sensors using typed dropdowns, plus physical ratings that HA does not supply. Missing/incomplete Energy setup blocks the wizard with instructions. Known sources and a usable history-based annual estimate are not asked again. Confirm grid import/battery discharge as positive, with inversion switches when necessary. Reopen the same app wizard to change sources. No helpers or physical-control automation are created. The app stores the configuration and issues a new revision. The HA entry stores only its connection and existing enable intent.

The app-owned installation wizard joins source selection, physical limits and routes. See [installation planning](INSTALLATION-PLANNING.md). Optional source keys and installations without a battery have no implicit SoC requirement.

The legacy source API retains optional household/PV bindings and configurable timestamp/age fields before an installation exists. Once installation settings are present, they derive required grid and per-battery roles. Competing `/v1/sources` edits are rejected. Household/PV forecasting definitions are configured separately through the app wizard. No native source form remains.

| Boundary | Responsibility |
|---|---|
| HA `SourceBinding` / `ObservationBridge` | Read selected sensors in process, normalize units/signs, serialize readings/issues and timestamp provenance |
| App `home_assistant/observation_wire.py` | Independently validate untrusted JSON, identity, quantities, normalized units, timestamps, duplicates and configuration revision |
| App `observations.py` | Frozen contracts, per-source readiness/deadlines and snapshot assembly |
| App `live_inputs.py` | Coordinate latest reports and independent forecast/price provider. Invalidate known-bad inputs and limit recalculation |
| Existing planning service/worker | Solve outside the HTTP loop, independently validate and publish a complete current candidate |

The same local Bearer-authenticated connection carries `PUT /v1/sources` and `POST /v1/observations`. Configuration writes compare the revision originally opened by the editor. Stale writes require reloading. Every selected source needs exactly one normalized reading or explicit issue. App-owned required flags cannot be overridden by a report. Zero remains a measurement, not a missing-data fallback.

Source replacement persists the new configuration and clears the active plan pointer in one atomic repository write before installing the runtime revision. A failed write leaves the acknowledged configuration, report and valid plan intact, allowing a retry with the same expected revision. When synchronization reports no measurement configuration, the integration clears its previous observation bridge and adopts the app's current revision, including a change back to synthetic mode.

Live replan refresh shares the lock used by publication and repository writes, including threaded saves. Input invalidation therefore cannot race a publication rollback into restoring an obsolete plan. Exceptions from an already invalidated calculation/publication retain the authoritative not-ready reason. A failure in the current round still reports a planning error.

HA revocation retains the invalidated plan's generation timestamp even after clearing its command pointer. Recovery cannot accept that generation or an older one again, including when a sensor recovers before app connectivity returns. An observation response with a changed configuration revision revokes commands immediately before being rejected. Synchronization must reconcile the new configuration before accepting a fresh plan.

An otherwise valid revoked-generation response is expected during recovery. Its typed rejection leaves commands unavailable while synchronization still persists intent and evaluates current source diagnostics. Malformed wire documents remain validation failures. They are not hidden by the recovery path.

The nonblocking `BasisSource` cache-read port supplies a `PlanningBasis`: configured physical model, independently versioned forecast and price content and a validity deadline. Snapshot assembly replaces only the battery's initial energy from measured SoC. It does not turn measured grid/household/PV power into future energy. Partial current forecast intervals are clipped using their interval-average forecast power, preserving future boundaries and original prices. Plan archives include the actual canonical observations, source bindings, issues, measurement/forecast/price versions, immutable problem and hash used for that plan.

The production [price adapters](PRICE-INPUTS.md), [contract rules](CONTRACT-PRICES.md), [solar forecasts](SOLAR-FORECASTING.md) and [demand forecasts](DEMAND-FORECASTING.md) supply future inputs through the installation planning basis. Missing required inputs keep live planning not ready. Synthetic forecasts or prices never fill live-data gaps. See [installation planning](INSTALLATION-PLANNING.md) for setup and remaining boundaries. Tests inject explicit future-input fixtures to isolate measured-SoC planning.

Reports are normally forwarded during the existing five-second synchronization. New reports must advance capture time, arrive within 30 seconds and retain each original sample/report timestamp. Required capture validity defaults to 300 seconds. An unchanged HA state does not expire by its original timestamp. Explicit device sample attributes are preserved as provenance, not used as heartbeats. Ordinary recalculation is provisionally bounded to once per five minutes. Changed configuration/future versions, input deadlines, explicit resume/replan and recovery can request a fresh calculation. No solver runs for each individual sensor change. These automatic defaults require further practical tuning.

A live candidate must finish calculation and reach publication before its first transfer interval ends: later battery states assume that transfer occurred. This candidate-admission check does not shorten the normal validity of a plan already accepted while its first interval was usable.

A known required fault, expired source/basis or source-configuration change invalidates current command publication and discards an in-flight superseded result. The integration also checks known local required faults before network access, so loss of the app connection cannot hide an already observed bad sensor. Live plan validity is capped by input deadlines. Without a known fault, a temporary connection failure retains only commands within their original validity. Recovered inputs require a newly validated plan. Original plan files remain unchanged. After app restart the selected configuration remains, but retained plans/reports are not reactivated.

Configured installations now expose capability-driven per-device command entities through `device_sensors.py`, including withdrawal and actionable notifications. Synthetic development inputs advertise their explicit test battery using the same per-device projection. A configured battery-free installation never gains a battery entity from a missing catalogue.

Tests cover strict transport, stable HA reports versus old device samples, optional versus required faults, source changes, restart, missing forecasts, independently preserved versions/SoC, partial intervals, bounded replanning and invalidation while solving. Real HA tests exercise app configuration ownership, normalization/forwarding and local invalidation without physical hardware. Desktop/mobile Playwright tests cover the app-owned wizard and developer entry page. HA's actual config-flow tests cover its one-time connection and app link. No visual HA-frontend verification is claimed.

The local container health check accepts HA-mode setup without a plan: source/forecast readiness is visible in status rather than preventing the app from starting. Required source configuration faults expose affected entity IDs in an action-required HA notification and logs. Routine not-ready conditions are not presented as a crashed container. Storage access stays behind the typed observation-state port, with JSON serialization in the adapter.


The [contract-price adapter](CONTRACT-PRICES.md) wraps the future-input port with validated effective rates and retained rule/source evidence. Exact billing segments preserve tariff boundaries independently of control intervals.
