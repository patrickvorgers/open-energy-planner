# Contract and prices

Your contract translates market prices into what grid imports cost and exports earn. Price sources supply market evidence. Configure both before interpreting expected costs. This guide describes OEP's supported marginal electricity calculation, not a complete supplier invoice.

## Keep three quantities separate

| Quantity | Meaning | Where to inspect it |
|---|---|---|
| Market price | Published or forecast electricity price before your contract formula | **Planning > Price inputs** |
| Effective import/export rate | Market price evaluated through the applicable dated formula | **Settings > Contract** and the price charts |
| Optional extra export bonus | Additional qualifying revenue with its own conditions and allowance | Contract bonus settings and **Bonus budget** |

Negative prices are valid inputs. An effective export rate can be negative, meaning export costs money for that interval. A favourable import price does not permit fictitious household consumption or charging beyond physical limits.

## Review the effective price rule

1. Open **Settings > Contract** and select the intended rule.
2. Check its start date and whether it is active, scheduled or superseded.
3. Choose **Edit price rule** to change it, or **Add price rule** to create a dated replacement.
4. Enter import and base-export formulas using the supported formula help. Check their units and what is already included.
5. Inspect the server-evaluated examples at negative, zero and positive market prices.
6. Save the contract and wait for confirmation.

[![A dated synthetic price rule with import/export formulas and their evaluated example rates.](../assets/contract-rule.png)](../assets/contract-rule.png)

*Read the formula together with the rate examples. The bonus is a separate column and must not also be embedded in the base export formula.*

OEP's [formula reference](../development/CONTRACT-PRICES.md#supported-formula-language) defines the actual supported syntax. Use [the worked Dutch contract examples](../development/CONTRACT-EXAMPLES-NL.md) to understand building blocks, then check them against your own contract. Opening an example does not change saved terms. Do not assume that annual netting, tax refunds, subscriptions or investment costs are included.

The Home Assistant timezone governs calendar dates. A future rule does not rewrite the original prices saved with an older plan.

## Add a bonus only when your contract requires it

Keep the base export formula and extra bonus separate. Review qualifying price conditions, allowed hours, annual volume cap and reset date. The **Bonus budget** panel shows the remaining allowance, used volume and opening evidence.

Home Assistant history normally establishes the opening volume. If complete history is missing, use manual confirmation only when the panel offers that recovery. Confirm the amount against the displayed **Opening as of** measurement date. Do not enter today's estimate against an older baseline.

Planned export does not consume the measured annual allowance. OEP evaluates proposed scenarios with separate counters. Changing a rule does not reset actual exported energy or create a new allowance automatically.

## Inspect price sources

Open **Settings > Installation > Price sources**. Review source availability and the selected preference for each forecast lead bucket. **Automatic** is a source-selection choice. It does not mean a price forecast is guaranteed correct.

Published prices and forecast prices remain separate. On charts, solid and dotted segments identify the distinction. A source's current status is not proof that its prices cover the whole requested horizon. Missing coverage can shorten or block a plan rather than becoming zero-priced electricity.

If a preferred provider is unavailable, inspect the actual source and fallback shown by the screen. Provider fetching, screen refreshing and planning have separate schedules. Repeatedly refreshing the browser does not bypass provider access limits.

## Check the result

Return to **Planning**. Compare current **Price inputs** with **Original plan prices**. The former uses current cached prices and saved rules. The latter preserves what the selected plan originally used. Contract repricing can evaluate the old flows under changed terms, but it does not make that old plan optimal under those terms.

**Next:** [Understand forecasts](forecasts.md), then [read your plan](read-your-plan.md).
