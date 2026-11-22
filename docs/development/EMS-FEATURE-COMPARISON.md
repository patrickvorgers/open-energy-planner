# OEP feature and configuration decisions

This guide records OEP's design decisions and criteria for future extensions.
It does not add implementation commitments or change confirmed requirements.

Status below means either **existing implementation** with its referenced module,
**future candidate** requiring its own requirements/tests, or **do not adopt**.
Existing implementation does not imply completed product-release acceptance.
The [release boundary](../../design/FIRST-RELEASE-SCOPE.md) remains authoritative.

| Feature | OEP decision and status | Reason and evidence to retain |
|---|---|---|
| Two optimization strategies | Existing implementation: cost/import strategies in `optimization`, GOAL-01/09/10 | Keep fewer imports primary for consumption mode and money as the tie-breaker. Test negative prices, reserve priority and depleted ending stock. Do not silently blend objectives. |
| Dated tariff components | Existing implementation: generic formulas and separate bonus rules in `contract_*`, PRICE-03–06 | One effective-date model works across markets. Preview positive/zero/negative spot rates and preserve original contract versions. Keep contract formulas generic rather than adding a country-specific rule engine. |
| Conversion efficiency and power limits | Existing implementation: `installation` and HiGHS device/route models, MODEL-03–07 | Share physical limits and AC/DC losses across OEP and reference simulations. Verify multiple sources sharing an inverter, not just isolated device ratings. |
| Battery SoC bounds and recovery | Existing implementation: reserve recovery and independent validation, BAT-05–06 | Safety-related planning floors remain hard. Current measured stock is never fabricated. Physical protection remains in equipment/HA. |
| Battery wear cost | Existing implementation: optional wear cost per internal kWh discharged, BAT-07/12 | Keep the agreed zero default and proportional discharged-energy accounting. Account only for discharged internal energy and do not imply a physical lifetime prediction. |
| Terminal state of charge and dwell penalties | Keep existing automatic terminal value and consumption-stock correction, HORIZON-09 | Avoid arbitrary penalties or a mandatory end-SoC setting. An end target can prevent useful use of stored energy. Test energy retained beyond the horizon and unchanged cumulative-import accounting. |
| SoC-dependent charge-power limits | Future candidate | Useful where a fixed maximum repeatedly overstates feasible charging. Start with measured evidence and a small validated curve. Test interpolation, capacity/power limits and independent accounting. Do not add this advanced wizard input pre-emptively. |
| Flexible-load readiness and deadline | Confirmed individual-consumer increment. See [appliance architecture](APPLIANCE-ARCHITECTURE.md) | Readiness requires a future deadline and a usable learned or manual cycle profile. The profile supplies duration and energy. Manual kWh entry is an optional fallback, not a separate required per-cycle request. Preserve power bounds and total-household forecast exclusions and make infeasible deadlines explicit. HA still translates virtual commands. |
| Contiguous cycles | Existing implementation. See [shiftable appliances](SHIFTABLE-APPLIANCES.md) | Each candidate has one start and the complete consecutive profile. Validate contiguous profile energy, start alignment and deadlines. Once started, price changes do not interrupt the cycle or create another start. |
| Minimum-on time | Later extension to the individual-consumer increment | Add a separate minimum-on-time constraint only for a concrete supported device contract, with exact duration and start-count scenarios and quarter and sub-quarter boundary tests. Do not offer every solver parameter up front. |
| Thermal and EV requests | Confirmed follow-on increments. See [EV charging](EV-CHARGING.md) and [thermal storage](THERMAL-STORAGE.md) | EV charging uses departure and energy intent. Electric boilers use their own thermal model and limits. Heat-pump forecasting does not establish support for heat-pump comfort control. Keep each device contract explicit and retain the agreed first-release boundary. |
| Planned-outage reserve | Future candidate, outside the current release boundary | A dated required reserve can be valuable. It differs from the existing minimum floor. Require start/end/target semantics, feasibility warnings and tests showing the selected objective cannot trade it away. No outage guarantees from a forecast. |
| Discharge and grid-charge restrictions | Do not add blanket policy switches now | Current confirmed physical constraints, reserve and optimization permissions already define dispatch. A future contractual restriction must become an explicit hard rule with negative-price tests, not a hidden heuristic. |
| Manufacturer-specific physical controls | Do not adopt in the app, ARCH-01/05 | HA and user automations own physical protocols. Keep explicit capability, applicability and unavailable semantics instead of accumulating vendor drivers. |
| Virtual-device commands | Reuse OEP virtual devices, ARCH-05 and GRID-13 | No extra user helper sensors. Separate actual power, fixed target and grid-following mode. Never disguise an inapplicable target as zero. |
| Configuration-change history | Existing immutable configuration/contract/input versions. Future user-facing change view | Current archives provide traceability. A readable audit view is useful later, but does not justify duplicating data or rewriting historical expectations. Separate settings changes from execution evidence. |
| Balance, price and financial views | Existing `savings` evidence and interactive overview, UI-01/20 | Keep forecast, plan and realization distinct. Show spot/effective rates, SoC and both references on matching UTC intervals. Do not turn factual reached limits into causal optimization explanations. |
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
