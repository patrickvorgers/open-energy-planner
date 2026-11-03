# Connect Home Assistant

Use an isolated Home Assistant environment before connecting a real installation. This route requires an accepted installation bundle and more preparation than the standalone demo.

## Start with a verified package

Install Python and Docker with Linux containers. Obtain the original `installation-bundle-amd64` ZIP from a successful candidate whose tested source was merged onto `main`. Keep the archive intact. You also need a clean checkout of that actual verified merge and GitHub read access.

Follow the [developer environment instructions](../development/DEVELOPER-ENVIRONMENT.md#start-and-stop) to verify and accept that bundle, then start the environment. The guide owns the exact update command, provenance requirements and recovery procedures. A current development checkout or a successful Quick check alone is not an installable bundle.

**Check:** package preparation and startup complete successfully. `python -m script.supervisor.dev status` lists the running containers, but does not verify package acceptance. If bundle verification fails, stop and correct the package or checkout mismatch. Do not copy loose source files over the installed app.

## Open the actual app

Open [the local Home Assistant app](http://localhost:7123/app/local_open_energy_planner). Sign in with the generated developer account recorded in `.local/dev/developer.json`. Do not publish that file. The default environment runs actual Supervisor and Home Assistant with simulated devices and history.

Open OEP from Home Assistant. In this Supervisor route, ingress authenticates the browser and pairing is automatic. You should not have to copy an app token or provide a Home Assistant user token. Standalone installations have a separate explicit pairing flow. See [automatic pairing](../development/AUTOMATIC-PAIRING.md).

**Check:** the app opens and its component build identities match the accepted package. If you see a token prompt in trusted ingress, inspect the connection and logs instead of pasting unrelated credentials.

## Review installation settings

Open **Settings > Installation**. The environment fills only missing sample settings, so a fresh laboratory may already have a configured example. Choose **Edit installation** to inspect an existing definition. A new or incomplete definition opens the wizard.

Review the Home Assistant connection and Energy sources. Confirm the maximum total grid import and export in kW. Review each detected solar source and battery. Zero solar sources and zero batteries are valid. Annual whole-home consumption is required only when usable history is insufficient. It is an estimate in kWh/year, not a current meter reading.

Use **Review detected settings** when offered. Check the complete installation before choosing **Save installation**. Wait for saved confirmation. A read-only refresh must not be mistaken for a save. See the [installation guide](../development/INSTALLATION-PLANNING.md) for exact field meanings and supported device arrangements.

**Check:** the saved installation reflects the confirmed sources and limits. Resolve unavailable or incompatible meters in Home Assistant Energy rather than guessing a unit or power direction.

## Review prices and the first live-input plan

Open **Settings > Contract**. Verify the active tariff rules and dates against the intended test contract. Sample laboratory rules are examples, not supplier recommendations. Review the price-source choices and availability described in [price inputs](../development/PRICE-INPUTS.md).

Open **Planning**. A usable plan requires accepted measurements, demand and solar forecasts as applicable, contract inputs and sufficient price coverage. External provider availability can delay or shorten the planning window. No synthetic fallback replaces missing Home Assistant data.

**Check:** an original plan shows its version and generation time, and its input diagnostics correspond to the configured installation. Read [your plan](read-your-plan.md) before interpreting commands. If the plan is missing, use [troubleshooting](troubleshooting.md).

## Keep control separate

The laboratory meters and tasks are simulated. A battery command does not update the stationary demo battery's state of charge. Real device operation requires separately reviewed automations and device safeguards. Use the [virtual-device contract](../development/VIRTUAL-DEVICES.md) before wiring any physical action.

Stop the laboratory with `python -m script.supervisor.dev stop`. That retains its data. Do not reset or overwrite an existing laboratory to make an example match this guide.
