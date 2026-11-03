# Price forecast selection and extended valuation prices

## Responsibility and precedence

The price boundary combines Nord Pool, EnergyPriceForecast EU and the public
EpexPredictor API, plus Jeroen.nl for the Netherlands, into typed price timelines. `ContractBasisSource` asks this
boundary for composed prices. The engine never selects providers, reads provider
JSON or calculates forecast errors. Every selected interval retains its original
source ID/version, classification and native resolution. Original plans are not
recalculated when a source preference changes.

**Published Nord Pool prices always win**, regardless of delivery lead time.
Publication coverage varies with collection time. 0–24 hours is not a fixed
published-price boundary. Forecast preferences only apply to unpublished hours.
Never replace a known price with a statistically better prediction.

The command horizon remains 48 hours: 96 quarters followed by 24 hours. Request
up to 72 hours from EpexPredictor to supply the existing up-to-24-hour terminal
valuation window beyond the command horizon. This does not add executable steps.
EnergyPriceForecast supplies its existing free 48-hour forecast. No API key, subscription or locally hosted prediction model is introduced. SQLAlchemy and its PostgreSQL/MariaDB/MySQL drivers provide the agreed storage abstraction.

## Adapter modules

### Automatic published-price route

The app uses its existing complete published-day cache first. When a day or its
scheduled correction is due, automatic selection prefers the official Home
Assistant Nord Pool integration. It must be loaded, expose
`nordpool.get_prices_for_date`, and uniquely match the saved contract bidding area
and EUR currency. Other HA price integrations and arbitrary sensor attributes are
not supported. The app-side bridge contract carries explicit area, currency,
delivery dates and request identities. Provider discovery and the public action
stay in the HA adapter. New providers can use this same exchange without changing
the planner.

With no configured, loaded, uniquely compatible HA source, use the existing direct
Nord Pool adapter. A stale HA report after two minutes also permits direct access
on the next scheduled refresh. Startup allows two minutes for discovery before
direct retrieval on the next retry. Retries remain at most once per 30 minutes.
A selected HA source with missing publication or an action failure retains cached
prices and retries HA. It does not trigger a second direct request for the same
failure. Forecasts still fill only unpublished hours. Existing forecast preferences,
original plans, stored source IDs and immutable financial evidence remain intact.

HA Nord Pool refreshes missing delivery days hourly. Its quarter-hour entity update
does not mean quarter-hour HTTP polling. OEP waits for the registered diagnostic
`tomorrow_price_available` binary sensor to turn on before requesting tomorrow.
Entity IDs are resolved through the entity registry. If the diagnostic is disabled
or absent, wait until 14:05 Europe/Oslo, allowing an hourly refresh after usual
13:00 publication. This fallback is a heuristic, not a guaranteed publication time.
An existing unavailable/off diagnostic continues to block premature requests.
Price actions run in an owned background task so they cannot hold command polling.
After snapshot release and intent checks, the bridge starts before the remaining
measurement and EV synchronization. Later domain failures in that pass cannot
suppress price exchange.
App report queueing is atomic on the event loop and never waits behind provider
network or storage work. Durable admission stays with the existing refresh owner.
Responses are normalized from EUR/MWh to EUR/kWh and must cover the complete
23, 24 or 25-hour market day. Each response is bound to its requested correction.

Settings shows the automatically selected route and reason separately from actual
published coverage. The route indicates the next retrieval path, not a claim that
existing cached values were downloaded through that path.

HA Core 2026.10.4's action makes a provider request even when its coordinator already
has prices. Reusing HA therefore does not guarantee fewer HTTP requests on that
version. Verify cache behavior against the supported HA version before relying on fewer requests. No fixed public-endpoint rate limit is assumed here.

Upstream references: [coordinator](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/nordpool/coordinator.py),
[availability](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/nordpool/binary_sensor.py),
and [public action](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/nordpool/services.py).

### Existing provider adapters

Each provider has its own source module in `prices/adapters/`: `nord_pool.py`, `energy_price_forecast.py`, `epex_predictor.py` and `jeroen.py`. All compose the shared `JsonClient` from `json_client.py` for bounded HTTP/timeouts and sanitized transport failures. Shared JSON normalization is in `_parsing.py`. Validated archive documents are handled by `store.py` and the shared SQL document adapter. Existing source protocols define the common typed contract. No provider inherits another provider or exposes native response fields to the engine.

## Matched evidence

All configured forecast sources refresh hourly, independently of replanning. A failed source retries after 30 minutes without re-fetching a healthy source. Epex responses remain usable for two hours, leaving a normal refresh and a failure retry before expiry. Receipt age is not a model-issue timestamp. Record only forecasts
actually received in the same successful refresh. Never reconstruct old forecasts
with today's model or realized weather. Normalize complete UTC delivery hours to
time-weighted mean EUR/kWh. Incomplete hours are excluded. Both sources must cover
the same target hour. Exclude hours already published, including actuals retained
from earlier refreshes. EpexPredictor also marks known quarters: discard every
hour containing one before admitting its forecast points.

Store the first matched pair per collection hour/target/lead bucket. Preserve both
original source-version references and prices. Repeated polling cannot replace
that pair with a later, more accurate estimate. Different hourly collections are
retained. Compare errors from identical pairs, average absolute errors within each
delivery hour, then give each delivery hour equal weight in the final MAE. Thus
opposite signed errors cannot cancel, and frequently sampled hours do not receive
extra weight. This compares service forecasts as received at the same decision
time, not models with identical underlying issue times (Epex does not expose one).

Maintain independent decisions for each bidding area and lead bucket:

| Lead at receipt/use | Purpose |
|---|---|
| 0–24 hours | Only unpublished gaps, when any exist |
| 24–48 hours | Forecast part of the executable horizon |
| 48–72 hours | Terminal battery-energy valuation only |

Only the overlap can produce a comparative score. For Dutch 48–72-hour
valuation, compare EpexPredictor with Jeroen.nl. EnergyPriceForecast's free request
is limited to 48 hours. Initial preference is EpexPredictor in that bucket and
EnergyPriceForecast in earlier buckets. This is an availability default, not
evidence of superior accuracy. An inherited default without the minimum matched
evidence does not override the newly covered tail default. Its old assessment is
retained unchanged. Other markets use only their eligible sources.

## Provider eligibility contract

Each forecast adapter owns `SUPPORTED_MARKETS` and `HORIZON_HOURS`. Its fetch
boundary rejects unsupported markets before any HTTP request. The composition
root instantiates only market-eligible adapters and passes their IDs and horizons
to source selection. The browser uses that same `available_sources` list. It does
not duplicate provider names or country rules. Preference writes reject sources
outside this list before changing memory or durable documents.

| Provider | Supported bidding areas | OEP request horizon |
| --- | --- | --- |
| Jeroen.nl | NL | Shared price horizon, currently 72 hours |
| EpexPredictor | DE-LU, AT, BE, FR, NL, SE1–SE4, DK1–DK2, ES, PT | Shared price horizon, currently 72 hours |
| EnergyPriceForecast EU | DE-LU, NL, BE, FR, CZ, PL, FI, SE1–SE4, DK1–DK2, AT, NO1–NO5, CH, ITN, IT_CNOR, IT_CSUD, IT_SUD, IT_CALA, IT_SICI, IT_SARD, SK, RO, ES, PT, GR, BG, GB | Smaller of the planning horizon and free 48-hour limit |

The market lists were checked on 27 October 2026 against the
[EnergyPriceForecast prices endpoint](https://api.energypriceforecast.eu/api/v1/home-assistant/prices?country=invalid)
and [EpexPredictor OpenAPI](https://epexpredictor.batzill.com/openapi.json).
Jeroen's adapter accepts only the Dutch forecast. DE-LU maps to each provider's
German alias. Regional zones remain distinct. EnergyPriceForecast requests EUR
explicitly, including markets whose default response uses NOK, DKK or GBP.
Market eligibility does not relax the existing EUR/kWh, market identity,
freshness or forecast-only validation. Supported markets do not guarantee that
every requested interval currently has usable coverage.

Unsupported markets disappear from all forecast dropdowns. An eligible source
with too short a horizon remains visible but disabled in that window. Automatic
remains available wherever forecast controls exist. A market with one forecast
uses it without fabricated comparison evidence. Its unsupported tail remains
unpreferred and uses the existing valuation fallback. With no supported forecasts,
there are no forecast-source controls. Published prices still work and uncovered
intervals remain unavailable. No forecast is substituted from another market.

Temporary outages, stale responses and partial coverage never change capability
lists or erase manual preferences. Unexpired eligible alternatives fill gaps as
before. Previously cached forecasts from a source no longer eligible for the
market are detached from the runtime cache, without deleting immutable archives.
Stored manual choices for registered but now ineligible sources retain their exact
revision and values. They act as Automatic while ineligible. The API separates
effective `preferences` from exact `stored_preferences`. The selected UI marker
reads **Automatic: Keep saved preference** and explains the inactive saved source
outside the options. The unsupported provider is never an available option.
Saving another bucket preserves that marker's exact stored value. Selecting
Automatic or an eligible provider explicitly replaces it. Backend validation
permits an ineligible value only when it is unchanged in its original bucket.
The existing transactional revision check still rejects stale writes. Saved
choices resume if the source becomes eligible again, unless the user explicitly
saves a replacement. Unsupported recommendation state is likewise
ignored in memory rather than deleted. An eligible winner survives when only its
old comparison partner becomes ineligible. Obsolete pair scores and streaks are
not presented as current evidence. A restoration failure remains visible in
all markets until a successful reload confirms recovery. No comparison or global
decision write may overwrite recovery evidence after an initial restoration
failure. Temporary later assessment-write failures remain retryable and do not
require a restart.
Having no comparison to score does not establish successful storage recovery.
A subsequent explicit preference save
uses the original revision guard and preserves the old immutable revision.

When adding or changing a provider, update its own market and horizon metadata,
fetch guard and provider-specific aliases. Verify the current public contract,
units, currency, forecast classification, issue time, expiry and usage limits.
Add composition and request-boundary regressions, including single-source and
zero-source markets, server-side preference rejection and responsive dropdown
behaviour. Do not infer permanent eligibility from a single successful or failed
HTTP response. Provider contract changes require a reviewed code change, not a
runtime rewrite of user settings.

## Named policy and planning horizon

`planning_policy.py` owns `PLANNING_HORIZON_HOURS`, fine/coarse resolution and the terminal window. Both solar and demand use its shared grid. Synthetic planning, price composition, bonus/contract validation, forecast evaluation and weather coverage read the same policy. Step counts and extra price coverage are derived. Changing the planning horizon from 48 to 60 in an isolated test produces 132 decisions and requests 84 price hours without editing consumers. Provider free-access/availability limits remain separately named bounds and can shorten actual coverage. Changing a constant does not fabricate missing prices/weather or remove physical/admission limits.

`prices/policy.py` owns MAE thresholds, evidence window/minimums, streak length and retry intervals. The status API includes the policy so UI text follows actual settings. These are code constants, not extra wizard options.

## Stable daily choice

Assess once per UTC day using delivery hours in the preceding 30 completed UTC
days. Require at least seven distinct settled days **and 168 matched delivery
hours**. Initial preferences follow the coverage defaults above. Missing evidence never
implies one provider is more accurate.

Switch only when the alternative's MAE is both:

- at least 10% lower than the current preferred source.
- at least EUR 0.001/kWh (0.1 ct/kWh) lower.

Both conditions must hold at three consecutive daily assessments. Repeated hourly
checks and restarts do not advance the streak. Missing assessment days, insufficient
evidence or a failed threshold reset it. Ties retain the current preference.
The same rule applies to switching back. These are explicit initial policy values,
not statistically proven optimal thresholds or a significance test.

Example: MAE 2.0 versus 1.8 ct/kWh meets both thresholds. MAE 0.4 versus 0.35 does
not meet the absolute threshold. MAE 2.0 versus 1.9 fails the relative threshold.

With three sources, maintain every independent source pair. Compare each
challenger directly against the current automatic preference on that pair's own
matched samples. Apply the existing thresholds and daily streak policy. If
multiple challengers qualify, choose the greatest absolute MAE improvement, with
a deterministic source-ID tie break. A changing challenger restarts its streak.
Do not rank raw MAEs from different sample sets. Show each pair's coverage and
sample counts alongside its scores.

## Availability and fallback

Within each lead bucket, prefer the selected source wherever it has usable
coverage. Another unexpired forecast fills missing coverage immediately, without
changing the statistical preference or waiting three days. Published prices still
win. Gaps stop the composed timeline. Missing prices never become zero.

When EnergyPriceForecast wins the overlap, use it there and Epex for the additional
tail. When Epex wins, use Epex for unpublished overlap and later prices. If no usable
post-horizon prices exist, the unchanged valuation method repeats the last up-to-24
hours of effective contract prices (24–48 becomes a proxy for 48–72 for a full plan).
If a partial later tail exists, valuation uses that contiguous available tail. It
does not invent a complete third day. See [terminal valuation](TERMINAL-VALUATION.md).

## Status, persistence and failure behaviour

Installation settings has **Price sources**, refreshed every 30 seconds.
The panel names Nord Pool as the fixed published-price basis and shows the end
of its contiguous known coverage from now in the Home Assistant timezone. Missing
published coverage and failed status reads are explicit. Lead buckets count hours
ahead from now, not calendar days. Source choices apply only to forecasts for
unpublished hours, even within the first bucket. This display uses the composed
price intervals and does not change source precedence or saved preferences.
For each lead bucket it shows published-price use, actual forecast source(s),
statistical preference, the reason for retention/switch/fallback, MAEs in ct/kWh,
matched hours/days, assessment date and pending challenger streak. The same typed
status is available in authenticated `GET /v1/prices` under `forecast_selection`.
These are currently selected cached prices, not a claim that an old active plan
has already been replaced. Saved plan tariff evidence remains authoritative for
that plan's actual inputs. No extra HA helper or entity is required.

The active app database retains pairs, hourly published truth and daily decisions
indefinitely. Normalized source versions remain in the existing immutable archive.
The 30-day window limits scoring, not retention. The selection policy depends on a typed storage port, not SQLite or SQLAlchemy.
`prices/storage/adapters/selection_store.py` implements that port using SQLAlchemy Core,
short explicit transactions and portable schema/queries. The default engine uses
the app-owned active database engine: SQLite by default, or configured PostgreSQL
(`psycopg`), MariaDB or MySQL (`pymysql`). Forecast pairs, published actuals and
selection decisions belong to the shared SQLAlchemy schema and move with all other
OEP tables during [backend migration](PLAN-ARCHIVE.md). All durable normalized price,
source, configuration and model versions use the same database. The same contract
tests run against all four real backends. No server failure silently activates an
empty local database. Transfers preserve original forecasts and decisions without
recomputing historical assessments. Unreleased development files are not imported.
Test installations are configured afresh.
For SQLite, include its consistent database and private bootstrap files in app-data
backup. External deployments back up the database consistently and retain matching
bootstrap credentials/connection settings. Logs remain separate operational files.

All comparison I/O runs off the event loop. Shutdown drains the owned writer.
An assessment storage failure retains the in-memory preference and reports a source
issue. It does not destroy evidence or claim a successful assessment. An unreadable
database on restart reports the issue and uses the initial default until recovery.
It cannot recover a preference from unreadable bytes. Unavailable/expired providers
leave usable cached prices available within their original validity.

## Indexed reads with indefinite retention

The scoring index `oep_price_pair_targets` uses `(area, bucket, target)`:
market area and lead bucket are equality predicates, followed by the UTC target
range. The actual-price primary key `(area, target)` supports the join. A daily
assessment scores only matches in the configured 30-day window. Older evidence
remains archived. This is a logical scoring boundary, not a physical-I/O limit.
The optimizer chooses its access path. PostgreSQL may scan all retained actual
prices for the area while using the forecast-pair range index.

Current source preferences use the existing decision primary key
`(area, bucket, day)`. For each configured lead bucket, the adapter requests its
latest row with `ORDER BY day DESC LIMIT 1`. Three buckets return at most three
rows, including when their latest assessment dates differ. It does not fetch or
sort all retained daily decisions. No extra decision index or cached status table
is needed.

On startup, the serialized storage worker verifies the named scoring index.
Existing tables are skipped by SQLAlchemy `create_all`, so a missing or older
two-column definition is explicitly created/rebuilt before reads. This changes
only the index, never evidence, preferences or archived plans. Failed DDL follows
the existing sanitized storage-fault/retry path. No data conversion or general
schema-migration framework is introduced.

`tests/database/contracts/test_price_history_performance.py` runs on the shared four-backend matrix.
It retains five years of hourly actuals, forecasts in three lead buckets with two
cohorts each, and daily decisions. It checks exact recent-window MAEs, staggered
latest decisions, unchanged historical row counts, bounded database results and
native query plans. The two-column-index repair test retains an original forecast
receipt, including an injected creation failure followed by a successful retry. Reports in `results/ci/price-history-*.json` contain native plans and
seven warmed timing samples for current reads versus the former full-history
fetch. GitHub Actions preserves them alongside test reports. Timings are evidence,
not hardware-independent CI limits or production latency guarantees.

## Provider limitations and proof

The public [Epex API](https://epexpredictor.batzill.com/docs) is fair-use with no
availability/correctness guarantee. Request `region`, `EUR_PER_KWH`, UTC, hourly
prices, zero surcharge and zero tax explicitly. Its response has no area/currency
echo or model issue timestamp. Request identity is the available evidence. Keep
`issued_at=null` and a two-hour response-use bound. Repeated receipt does not prove
the underlying model was updated. Supported regions are DE-LU (API DE), AT, BE, FR,
NL, SE1–SE4, DK1–DK2, ES and PT. Actual service availability may be narrower.
Unsupported areas do not construct or offer EpexPredictor. They use only the
other market-eligible sources and do not report an Epex fetch failure.

Epex's [published backtests](https://github.com/b3nn0/EpexPredictor#model-performance)
use historical weather. EnergyPriceForecast's
[quality results](https://energypriceforecast.eu/en/forecast-quality/) use frozen
forecasts and later actuals. Their headline MAEs are not comparable evidence for
selecting a current winner. We collect matched original responses instead.

Independent tests cover MAE arithmetic, thresholds, day/window/area/bucket isolation,
restart/failed-storage behaviour, published-price priority, normalized provider
validation, actual-versus-preferred status, extended valuation and desktop/mobile
display. The persistent laboratories use the normal configured price adapters
with simulated meters and consumption history. Isolated fixtures provide
deterministic forecast evidence, including a 72-hour tail. Laboratory operation
does not establish forecast accuracy or actual household savings.

The [plan archive](PLAN-ARCHIVE.md) stores plans and shared input snapshots through SQLAlchemy. All durable configuration/source/model archives share that database. Unreleased development files are not imported.

## Jeroen.nl boundary

The public `https://jeroen.nl/mcp` endpoint uses bounded JSON-RPC POST requests,
without an MCP SDK or credentials. It receives MCP initialization metadata
(protocol version, capabilities and OEP client name/version) and the forecast
request, never Home Assistant coordinates, entities or personal configuration. Request native
quarter-hour bare market prices for five calendar days, bounded by the provider's
seven-calendar-day capability, then clip to the shared 72-hour price horizon.
Calendar coverage includes today and is not a rolling 168-hour promise.

Use `time_utc` through DST folds and P50 as deterministic input. Validate the
P10/P90 band, but do not use it for stochastic optimization. Discard known-price
rows. Nord Pool remains the published-price authority. Preserve the actual daily
`forecast_made_at_utc` separately from receipt. Admit a model for at most 27 hours
after issue. A later HTTP receipt cannot renew an old model. Hourly refreshes and
30-minute failure retries remain below the anonymous access limits.

### Available interfaces and transport choice

Public documentation checked on 27 October 2026 distinguishes these interfaces:

| Interface | Access and documented data | OEP assessment |
| --- | --- | --- |
| Public MCP at `https://jeroen.nl/mcp` | No account or key. Includes electricity price forecasts with uncertainty bands. | Implemented forecast interface. OEP calls `initialize` and `tools/call` for `get_electricity_price_forecast` directly over HTTP. No AI agent or MCP SDK is involved. |
| Account download feeds, reached through `https://jeroen.nl/account/data/` | Account and personal API key. JSON, CSV and XLS downloads. The public description lists electricity prices for today, tomorrow and historical years, gas history and a solar forecast. | Not implemented. Published electricity prices and solar forecasts do not establish availability of electricity price forecasts. The account page is an entry point, not a documented REST request URL. |
| Account MCP at `https://jeroen.nl/mcp/account` | Account authorization through OAuth 2.1. Personal energy data. | Not needed for public price forecasting. OEP does not request account access. |
| Separate public REST price-forecast API | No public request contract was established by this documentation review. | Unconfirmed, rather than proven nonexistent. Do not invent an endpoint or depend on an internal website request. |

Sources: [account data and download feeds](https://jeroen.nl/account/data/),
[API and MCP terms](https://jeroen.nl/api-voorwaarden),
[general terms, article 12](https://jeroen.nl/algemene-voorwaarden) and the
[forecast description](https://jeroen.nl/dynamische-energie/stroom/prijzen/voorspelling).
Account-only documentation and keyed download responses were not inspected.

MCP is itself an API protocol, but it is not interchangeable with a REST request
contract. Keep the existing public MCP adapter because it supplies the required
forecast data without account configuration. There is no demonstrated data,
availability or performance advantage from switching to an unconfirmed REST
interface. This is an architectural decision, not a claim that MCP is inherently
faster or more reliable. Reconsider only if a supported alternative documents
equivalent quarter-hour forecasts, units, model issue times, uncertainty bands,
coverage, limits and permitted use.

Anonymous MCP limits are currently 60 requests per hour and 20 per minute per IP.
Account download feeds have a separate daily allowance under article 12 of the
general terms. Do not apply the MCP allowance to a keyed download feed. These
limits are a dated provider policy, not an availability guarantee. Recheck them
when changing the adapter or preparing a release.

## Manual preference and quality visibility

Choose Automatic or an available source independently for 0–24, 24–48 and
48–72 hours. Manual preference affects forecast use, never published-price
precedence, evidence collection or the independent automatic recommendation.
Each forecast adapter declares `HORIZON_HOURS` and uses that same value for its
request or clipping boundary. The composition root supplies these limits through
`source_preferences.source_horizons_hours`. EnergyPriceForecast EU is bounded by
the smaller of the planning horizon and its 48-hour free API limit. EpexPredictor
and Jeroen.nl request the full 72-hour price horizon. The UI disables a source
when its maximum cannot span the selected window, and the server rejects the
same choice before any preference write. Automatic remains available in every
window. Actual current coverage and expiry remain independent diagnostics.
An eligible preferred source can still be temporarily incomplete or unavailable,
in which case composition falls back to another usable forecast.

Previously saved preferences retain their exact revision and values on restore,
including older choices outside the provider limit. They are not silently
rewritten or treated as corrupt storage. Such a choice remains visible as a
disabled option with its horizon. Choose Automatic or a supported source for
that window before saving another preference edit. Existing forecast fallback
continues to protect uncovered intervals until the choice is updated.

The shared installation Edit, Save and Cancel controls own this draft. Polling
refreshes quality without discarding edits. Authenticated
`PUT /v1/prices/preferences` requires exactly `preferences`,
`configuration_version` and `market_area`. The server rejects stale revisions,
changed market areas, unknown sources and wrong bucket counts. Save confirmation
follows committed storage, with the same serialized persistence barrier as prices.

Display actual sources, mode, preference, automatic recommendation, fallback,
pending challenger, pairwise MAEs in ct/kWh, matched hours, settled days and
assessment date. Model issue, receipt and coverage times remain distinct.
The collapsed Forecast availability disclosure precedes the horizon blocks. Its
open state survives polling. Each source shows
availability, its supplied forecast issue time and the end of its last forecast
interval. Availability does not mean that the source is used. The horizon blocks
show actual use. A last interval does not prove gap-free coverage or accuracy.
Missing timestamps remain Not known. Details retain the OEP ingestion time,
which never substitutes for an absent issue time. Open details follow source
identity when polling reorders sources. Source failures clear the overview with
the other live evidence, without discarding preference drafts.
Configured providers without an accepted forecast remain visible as unavailable
with unknown timestamps. Individual provider diagnostics remain visible below.
These are statistical service comparisons, not proof of future accuracy or
measured financial savings. Missing evidence remains unavailable.

Keep the original EnergyPriceForecast/Epex pair in its existing tables and retain
all old assessments. Additional identified pairs use shared-schema comparison
tables. Per-market global recommendations and immutable manual revisions live in
the existing `prices` SQL document namespace. Backend transfers copy every table
and document without needing the source database afterward.
