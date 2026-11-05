# Before you start

Start by understanding the inputs and the current installation boundary. You can try a real synthetic plan without Home Assistant, then prepare an isolated installation before using your own measurements.

## Choose the first experience

| Your goal | What you need | Continue with |
|---|---|---|
| Understand the model and interface | This guide and the energy glossary | [How OEP works](how-oep-works.md) |
| Run a plan without equipment | Git, a supported 64-bit Python environment and a browser | [Try your first plan](first-plan.md) |
| Configure an existing OEP installation | The matched app/integration pair and access to Home Assistant Energy | [Connect Home Assistant](home-assistant.md) |
| Contribute code or test actual Supervisor behavior | The pinned tools and an accepted numbered package | [Developer start](../development/README.md) |

## Check current availability

OEP is a public development preview, not a stable product release. Public HACS installation is not available yet. Existing administrator-managed installations use matched app and integration artifacts. Do not combine independently chosen builds.

Linux AMD64 is the maintained full acceptance platform. Other Python installations depend on compatible native dependency wheels. An experimental install workaround is not platform acceptance. The [release scope](../../design/FIRST-RELEASE-SCOPE.md) records the remaining product gates.

## Prepare Home Assistant Energy

Energy owns your grid, solar, battery and individual-consumer source membership. Check **Home Assistant Settings > Dashboards > Energy** before OEP setup.

| Source | What to confirm |
|---|---|
| Grid | Every import/export cumulative energy channel and the configured normalized instantaneous power |
| Solar | Each disjoint production source, its units and any supported linked panel metadata |
| Battery | Separate charge/discharge energy, normalized power, SoC and usable capacity |
| Individual consumer | Its own cumulative energy and power evidence without overlapping a selected parent or another controlled device |

Power in W or kW is a rate. Energy in Wh or kWh accumulates over time. A power sensor is not a substitute for a cumulative settlement meter. Energy can normalize inverted or combined power readings. OEP reads that normalized result rather than applying the same inversion twice.

Grid power is import-positive and export-negative. Battery terminal power is discharge-positive and charge-negative. Missing or unavailable constituents of a combined power sensor must be repaired at the source, not treated as zero.

The household balance must use disjoint channels. Selecting both a total and its components can count energy twice. A source can be structurally configured before its current readings or history are usable. These are separate readiness checks.

## Prepare OEP-only settings

Have the actual grid limits, physical inverter relationships, charge/discharge ratings, reserves and relevant efficiencies available. If recent common energy history is insufficient, supply a reasonable annual whole-home consumption estimate. Empty history is not zero demand.

Have your contract's effective dates and import/export formulas ready. A bonus needs its own eligibility, allowance and reset definition. Do not copy laboratory tariffs or equipment limits as recommendations for your installation.

Home Assistant is the timezone authority. OEP has no independent timezone setting. Physical equipment protections and fast local control remain outside the optimizer.

## Know where help lives

Open **Help > Wiki help** in the app navigation. On a phone, open **Menu** first. The public guide opens in a new tab, so the current app page stays open. It needs internet access.

Keep tokens, private entity details, measurements and location data out of public screenshots or issue reports. Use the [troubleshooting guide](troubleshooting.md) for a specific failure rather than replacing missing inputs with invented values.

**Next:** [Try your first plan](first-plan.md), or [connect an existing installation](home-assistant.md).
