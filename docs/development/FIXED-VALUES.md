# Fixed-value and persistence ownership

Fixed policy uses meaningful uppercase constants at its responsible module.
Include units for prices, power, time and tolerance. Shared physical/time
conversions live in `units.py`. Shared horizon/grid policy lives in
`planning_policy.py`. The HA integration remains separately installable, so it
owns its wire-validation constants without importing the app package.

Apply named-value ownership to active Python production code and frontend policy. This includes security bounds, file permissions, scheduler/worker deadlines, forecast age/training thresholds, device defaults, solver validation tolerances, source polling, formula limits and archive hashes. Test inputs and independent arithmetic remain separately specified. Validate import wiring, runtime defaults and UI behavior through the affected suites. Source-equivalence checks alone cannot prove those contracts.

## One horizon owner

`PLANNING_HORIZON_HOURS = 48` is the single executable-horizon setting in code.
The first 24 hours use 15-minute steps. The remainder uses 60-minute steps.
Dependent step counts, source timelines, command-input validation, optional
terminal price coverage and weather requests derive from that policy. A separate
named provider access limit, such as EnergyPriceForecast's free 48-hour maximum,
must not be mistaken for the planner setting. Increasing the horizon cannot create
data outside a provider's available coverage. The normal shortened-horizon and
fallback rules still apply.

`tests/app/planning/test_planning_policy.py` changes only the horizon assignment in an isolated
Python process. It independently expects 132 steps and 84 price hours for a
60-hour plan, checks the actual demand/solar/synthetic consumers and price defaults,
and verifies bonus-window validation changes with it. The default DST case still
expects exactly 96 quarters and 24 hours.

## Persistence boundary

Price selection consumes a typed storage port. Its
SQLAlchemy adapter owns connections, typed tables, portable queries and atomic
transactions. SQLAlchemy native objects do not enter the planner or policy.
SQLite, PostgreSQL, MariaDB and MySQL execute the same storage contract tests in CI.
External connection failures never silently create a new local database.

All durable app stores share the active SQLAlchemy engine. Configuration,
source/model archives, counters, plans and evaluations join its schema and
complete cross-backend transfer. JSON document names identify database records,
not separate files. Credentials, certificates, backend binding metadata and logs
remain outside the database. See the [database dictionary](DATABASE-SCHEMA.md).
Direct SQLite usage in historical storage studies remains research evidence.
Dedicated test inspection of a fixture database is not application persistence.

See [the code standard](CODE-DOCUMENTATION.md), [price-source policy](PRICE-FORECAST-SELECTION.md)
and [technical storage design](../../design/TECHNICAL-DESIGN.md#backend-and-abstraction).
