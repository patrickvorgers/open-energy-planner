# Before you start

Choose a safe first experience. You can inspect a real OEP plan using synthetic data before connecting Home Assistant.

## What OEP does

Open Energy Planner runs a local optimization app beside Home Assistant. The app proposes how energy can flow through your installation over the planning window. Its thin Home Assistant integration exchanges measurements and publishes virtual commands.

A virtual command is a proposed action exposed in Home Assistant. It does not automatically operate your battery or another appliance. Physical device control, manufacturer limits and electrical safety remain with your own automations and equipment.

## Choose your starting point

| Your goal | What you need | Start here |
|---|---|---|
| Understand the interface and a plan | Repository access, Git, a supported 64-bit Python installation and a browser | [Local synthetic demo](first-plan.md) |
| Explore setup with Home Assistant | Repository access and an accepted, numbered CI installation bundle, plus Python and Docker with Linux containers | [Isolated Home Assistant](home-assistant.md) |
| Connect real measurements | An administrator-managed installation, configured Energy meters and the relevant integration knowledge | [Installation reference](../development/INSTALLATION-PLANNING.md) |

The demo needs no Home Assistant knowledge. For an actual connection, you should know how to open Home Assistant Settings, configure the Energy dashboard, identify entities and inspect their units and availability. A power reading in W measures a rate. An energy counter in kWh accumulates consumption or production over time.

## Current availability

The repository and documentation are public. The integration is not currently available through public HACS. The release packages and native acceptance procedures are described in the [installation acceptance guide](../development/HAOS-ACCEPTANCE.md). These are development procedures, not a one-click public release. Verification of a numbered installation bundle still requires GitHub read access.

The maintained full acceptance platform is Linux AMD64. Installing Python packages elsewhere depends on compatible native dependency wheels. If installation fails, use the supported Docker-based developer route rather than bypassing dependency checks.

## Before using real data

Check the [release boundaries](../../design/FIRST-RELEASE-SCOPE.md). Back up your installation using your established process. Begin in an isolated environment. Never copy a sample battery limit or tariff into a real installation without checking it against your own equipment and contract.

Home Assistant supplies the timezone. OEP does not have a separate timezone setting. Planning limits are not electrical protection. Missing or stale inputs may prevent a plan, and forecasts do not guarantee a particular outcome.

**Next:** [Try your first plan](first-plan.md).
