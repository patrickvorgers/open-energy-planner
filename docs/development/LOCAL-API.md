# Local API and integration boundary

Developer reference for the authenticated app/integration boundary. For ordinary setup, use the [user configuration guide](../users/configuration.md). These endpoint summaries do not authorize physical device actions.

HA OS exposes administration through authenticated HA ingress and pairs automatically through Supervisor without user tokens/codes. Standalone administration requires the app Bearer token and explicit pairing consent. Both routes use pinned TLS and a separate persistent HA credential restricted to synchronization routes. See [automatic pairing](AUTOMATIC-PAIRING.md). The explicit `--demo` mode optimizes synthetic inputs through the same installation model and schema-2 device commands as live planning. Empty archives contain no fabricated plan. Deterministic test schedules and injected solver results belong to test fixtures.

- `GET /v1/snapshot`: full accepted plan, release/configuration, enable intent, receipt acknowledgement and current calculation status/source ID. In HA mode, polling checks input validity and may request a rate-limited planning round when due.
- `PUT /v1/enabled`: boolean plus stable control request ID. Pause is immediate in HA. Resume requests a fresh optimizer round. Repeated delivery of one control request does not create repeated plans.
- `POST /v1/replan`: explicit synthetic source update (`initial_kwh`, optional first-slot `first_load_kwh`). Invalid input is rejected. In HA mode it requests a fresh calculation from accepted real measurements and the independent future-input provider. Synthetic initial-state fields are not used.
- `PUT /v1/sources`: HA-mode app-owned source configuration with expected-revision checks.
- `POST /v1/observations`: HA-mode normalized readings/issues with per-source timestamps and configuration revision.
- `POST /v1/history`: normalized hourly export evidence for the current app-requested missing bonus baseline, or an explicit read failure. Stale request versions cannot overwrite confirmed counters.
- `POST /v1/ack`: receipt of the current plan ID, never physical execution confirmation.

Battery positive W means discharge, negative W means charge. A non-applicable fixed target is `unknown`. Pause/expiry means `unavailable`. 0 W is a real idle command. An `open_energy_planner_command` event carries the coherent current command after entity writes. Filter by `entry_id`. Do not convert missing numeric states to zero. The active-plan timestamp diagnostic carries the current plan ID. Per-device target attributes contain command interpretation only, so accepting an identical command in a new plan does not retrigger target automations. Separate HA entities are not a general transactional system.

Plan generation may finish after the first covered interval began. The complete input snapshot/hash, version references, independent costs, checked flows and command side/resolution stay with the original plan. A failed round does not overwrite or prolong the last accepted plan. Plans, configuration, rules, source/model archives and evaluations use the shared active SQLAlchemy database. SQLite, PostgreSQL, MariaDB and MySQL share backend contracts and complete transfer tests. See the [database dictionary](DATABASE-SCHEMA.md). Full deployment backup and restore remain separate acceptance gates.
