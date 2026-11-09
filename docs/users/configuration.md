# Configure OEP

Configure sources and limits before interpreting a plan or connecting physical actions. This guide shows the order and the owner of each setting. Use the linked pages for the actual steps and field meanings.

## Work through the setup in order

| Step | What you establish | Continue with |
|---|---|---|
| 1. Home Assistant Energy | Grid, solar, battery and individual-consumer measurement identities | [Prepare your sources](before-you-start.md#prepare-home-assistant-energy) |
| 2. Installation | Connection limits, household scale, solar properties, battery topology and ratings | [Installation settings](installation.md) |
| 3. Planning strategy | Whether cost or corrected grid import is the primary goal | [Choose the goal](installation.md#choose-the-planning-strategy) |
| 4. Contract and prices | Effective dates, import/export rates, optional bonuses and provider availability | [Contract and prices](contract.md) |
| 5. Appliances, if present | Forecast membership, physical capabilities and explicit scheduling permissions | [Appliance settings and requests](appliances.md) |
| 6. First result | A coherent accepted plan with the intended sources and strategy | [Read your plan](read-your-plan.md) |

An installation without solar or a battery is valid. Do not create fictitious sources to satisfy an example. A heat pump selected for weather-sensitive forecasting is not thereby authorized for temperature control.

## Save deliberately

Saved settings normally open as summaries. Select the relevant area or device and choose its **Edit** action. Configuration pages keep the save state and actions together so they remain reachable while scrolling.

1. Inspect the saved values before editing.
2. Change only the intended settings. Explain any prefilled equipment defaults against your actual specifications.
3. Use the page's preview or validation when offered.
4. Choose the relevant **Save** action and wait for confirmed persistence.
5. Reopen the saved summary and check that it reflects the intended change.

**Cancel** discards the remaining draft and reloads saved state. It cannot undo an independent write that already succeeded during a partially failed save. Read the failure message before retrying. A refresh is not a save, and a saved setting does not prove that Home Assistant has accepted a new plan.

When you follow an internal link with unsaved configuration, **Leave without
saving?** offers **Cancel** to keep editing or **Discard and leave** to open the
chosen page. This applies to the main menu, appliance categories and notification
recovery links. A save or reload already in progress must finish first. Refreshing
the browser, closing its tab or using browser history can still show the browser's
own warning, whose appearance the app cannot customize.

## Separate setup from daily operation

**Settings > Appliances** defines equipment and permissions. The **Energy** pages contain daily requests such as an appliance deadline, an EV departure or a boiler comfort target. Those requests are explicit actions rather than replacement installation definitions.

**Insights > Forecast quality** explains prediction and model evidence. **Energy > Savings** evaluates retained measurements. Neither page is the place to change electrical ratings or contract terms.

## Confirm a coherent result

On **Planning**, check the generated time, plan version and strategy. Confirm that the expected sources appear, then inspect cost and battery behavior. A source warning or an unavailable comparison calls for investigation, not an assumed zero.

If no plan appears, use [troubleshooting](troubleshooting.md). For storage settings and preserving OEP history, see [Data storage](data-storage.md).

**Next:** [Installation settings](installation.md).
