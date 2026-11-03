# EMS feature and configuration reference

Checked: 24 October 2026. This is a product/design assessment, not a benchmark or
new implementation commitment. Public first-party documentation describes what
other systems expose. It does not prove their algorithms, accuracy or defaults.
No competitor code or test scenarios are copied.

## Evidence and deployment differences

- **DAO:** [current settings reference](https://github.com/corneel27/day-ahead/wiki/05.-Overzicht-alle-instellingen)
  and [basic configuration](https://github.com/corneel27/day-ahead/wiki/02.-Installatie-en-basis-configuratie).
  Its documented settings include price-driven versus import-minimizing strategy,
  dated tariff components, hour/quarter planning, battery power/SoC limits,
  staged conversion efficiencies, cycle cost, zero-grid balancing and boiler/EV
  parameters. Some settings can reference HA entities. The basic configuration covers dated tariffs and database connections. The
  settings reference covers controller/helper entities. Historical
  hourly-price wording is not evidence of today's exchange settlement resolution.
- **EMHASS:** [configuration reference](https://emhass.readthedocs.io/en/latest/config.html).
  Documented controls include deferrable-load energy/runtime, earliest start,
  deadline, contiguous profiles, minimum-on-time and optional load cost limits.
  Battery initial/final SoC, efficiencies, power bounds, grid charge/export
  restrictions, optional charge/discharge weights, SoC dwell penalties and
  charge-power derating. Multiple-battery parameters have scalar/list semantics.
  Some restrictions remain fleet-wide. These are reference capabilities, not OEP
  defaults or a claim that every constraint is enabled together.
- **GBBOptimizer:** [first-party release history](https://gbboptimizer.gbbsoft.pl/About)
  announces dated price definitions, battery usage cost/cycle reporting,
  scheduled-outage SoC preparation, IoT/EV conditions, manufacturer integrations,
  configuration-change history and forecast/profit views. Announcements do not
  expose full mathematical semantics or independently validate results.
  The [product site](https://gbboptimizer10.gbbsoft.pl/) describes a hosted service.
  The public [GbbConnect2 repository](https://github.com/gbbsoft/GbbConnect2) is a
  connector. Its availability does not establish the optimizer's source licence
  or prove a fully local optimizer deployment.

The links are moving references. Recheck the relevant page/field before implementing
an idea. No account, trial, payment, provider dependency or competing service is
required by this comparison.

## Fit with Open Energy Planner

Status below means either **existing implementation** with its referenced module,
**future candidate** requiring its own requirements/tests, or **do not adopt**.
Existing implementation does not imply completed product-release acceptance.
[the release boundary](../../design/FIRST-RELEASE-SCOPE.md) remains authoritative.

| Reference idea | OEP decision and status | Reason and evidence to retain |
|---|---|---|
| DAO's two objectives | Existing implementation: cost/import strategies in `optimization`, GOAL-01/09/10 | Keep fewer imports primary for consumption mode and money as the tie-breaker. Test negative prices, reserve priority and depleted ending stock. Do not silently blend objectives. |
| DAO dated tariff components. GBB dated definitions | Existing implementation: generic formulas and separate bonus rules in `contract_*`, PRICE-03–06 | One effective-date model works across markets. Preview positive/zero/negative spot rates and preserve original contract versions. No country-specific rule engine is added from this research. |
| DAO staged conversion. EMHASS efficiency/power settings | Existing implementation: `installation` and HiGHS device/route models, MODEL-03–07 | Share physical limits and AC/DC losses across OEP and reference simulations. Verify multiple sources sharing an inverter, not just isolated device ratings. |
| Battery SoC bounds and recovery | Existing implementation: reserve recovery and independent validation, BAT-05–06 | Safety-related planning floors remain hard. Current measured stock is never fabricated. Physical protection remains in equipment/HA. |
| DAO cycle cost. EMHASS usage weights. GBB usage cost | Existing implementation: optional equivalent-cycle compensation, BAT-07/12 | Keep the agreed zero default and proportional discharged-energy accounting. Do not copy another system's charge-plus-discharge unit convention or imply a physical lifetime prediction. |
| EMHASS final SoC and optional dwell penalties | Keep existing automatic terminal value and consumption-stock correction, HORIZON-09 | Avoid arbitrary penalties or a mandatory end-SoC setting. An end target can prevent useful use of stored energy. Test energy retained beyond the horizon and unchanged cumulative-import accounting. |
| EMHASS charge-power derating by SoC | Future candidate | Useful where a fixed maximum repeatedly overstates feasible charging. Start with measured evidence and a small validated curve. Test interpolation, capacity/power limits and independent accounting. Do not add this advanced wizard input pre-emptively. |
| EMHASS deferrable-load energy/deadline | Highest-value future device increment, MODEL-01–02 and deferred flexible consumption scope | A required kWh request, available time window and power bounds have clear user value. Preserve total-household forecast exclusions and make infeasible deadlines explicit. HA still translates virtual commands. |
| EMHASS contiguous sequences/minimum-on-time | Later extension to that increment | Appropriate for appliances that cannot interrupt. Add only for a concrete supported device contract, with exact duration and start-count scenarios and mixed-grid boundary tests. Do not offer every solver parameter up front. |
| DAO boiler/EV and GBB EV/IoT controls | Reuse the future generic flexible-load contract. Defer thermal/vehicle models | An EV energy deadline is simpler than a thermal comfort model. A heat pump also needs temperature dynamics, comfort bounds and physical validation. Neither is already supported by today's battery/PV planner. |
| GBB planned-outage reserve | Future candidate, outside the current release boundary | A dated required reserve can be valuable. It differs from the existing minimum floor. Require start/end/target semantics, feasibility warnings and tests showing the selected objective cannot trade it away. No outage guarantees from a forecast. |
| DAO/GBB discharge/grid-charge restrictions | Do not add blanket policy switches now | Current confirmed physical constraints, reserve and optimization permissions already define dispatch. A future contractual restriction must become an explicit hard rule with negative-price tests, not a hidden heuristic. |
| GBB manufacturer-specific controls | Do not adopt in the app, ARCH-01/05 | HA and user automations own physical protocols. Keep explicit capability, applicability and unavailable semantics instead of accumulating vendor drivers. |
| DAO helper/controller mappings | Reuse OEP virtual devices, ARCH-05 and GRID-13 | No extra user helper sensors. Separate actual power, fixed target and grid-following mode. Never disguise an inapplicable target as zero. |
| GBB configuration-change history | Existing immutable configuration/contract/input versions. Future user-facing change view | Current archives provide traceability. A readable audit view is useful later, but does not justify duplicating data or rewriting historical expectations. Separate settings changes from execution evidence. |
| DAO/GBB balance, price and profit views | Existing `savings` evidence and interactive overview, UI-01/20 | Keep forecast, plan and realization distinct. Show spot/effective rates, SoC and both references on matching UTC intervals. Do not turn factual reached limits into causal optimization explanations. |
| Broad provider, solver and database menus | Keep current defaults and adapter boundaries, ARCH-03/10 | Market area and contract are user choices. Internal solver/model knobs are not onboarding work. SQLite remains default with explicit supported backend transfer, not direct HA database access. |

## Criteria for extensions

1. **Additional flexible consumers** need a concrete energy/deadline or cycle contract.
   Reuse the [existing appliance and EV boundaries](APPLIANCE-ARCHITECTURE.md).
2. **Dated reserve request** only if the product owner wants planned-outage
   preparation. Keep it separate from everyday minimum reserve.
3. **SoC-dependent charge limits** only after actual forecast-versus-realization
   evidence demonstrates a material planning error.
4. **Configuration history view** when a user-facing investigation needs it.

These are recommendations, not additions to the agreed first release. Second-layer
counterfactual explanation remains separate and low priority.

## Acceptance checklist for any adopted idea

- State its owner: HA source/physical automation, app configuration, forecast,
  physical model, objective, financial settlement or presentation.
- Reuse the Energy dashboard catalogue and existing model classes. Ask only for
  missing meaningful properties. Distinguish device membership from permissions.
- Specify units, signs, dates, measurement boundaries and infeasibility behaviour
  before adding fields. Preserve current plans and a usable fallback.
- Add small independently calculated scenarios, including interaction with
  existing batteries/PV, negative prices, bonuses, shared limits and rolling
  horizon state where relevant. Passing a solver does not prove the oracle.
- Evaluate claimed benefit with frozen inputs and realized evidence. A new model
  must not manufacture historical forecasts or claim ML accuracy from a smoother
  chart. Keep external-service access/licence checks separate from data licences.

No new database schema, application configuration, commands or physical integrations
are introduced by this document. Documentation checks and latest-head review still
apply to its PR.
