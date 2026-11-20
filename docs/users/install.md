# Install Open Energy Planner

OEP has two parts. The **integration** runs inside Home Assistant and exchanges
measurements and planning advice. The **app** runs the planner and its interface.
Install both from the same release before configuring your installation.

## Requirements and availability

- Home Assistant **2026.11.0 or newer**. The Marketplace is built in. Do not install HACS separately.
- Administrator access to Home Assistant.
- For the app-store route below, **Home Assistant OS on AMD64 hardware**.
  ARM systems, including Raspberry Pi and Home Assistant Green, are not supported
  by the current app package. Installing the integration alone does not provide a planner.
- A GitHub account to add OEP as a custom Marketplace repository.
- A published OEP release with completed checks and installation assets.

OEP remains a development preview. The installation channel becomes usable after
the first tested stable release is published. Check the
[releases page](https://github.com/patrickvorgers/open-energy-planner/releases)
for `open_energy_planner.zip`, `ha-app-repository.tar.gz`,
`release-manifest.json` and `SHA256SUMS`. If no stable release with those assets
exists, stop here. Adding the source repository cannot substitute for a release.
Publishing a release is separate from merging a pull request.

The Marketplace works on Home Assistant Container too, but Container has no
Supervisor app store. Its planner container must be managed separately. A beginner
installation guide for that route is not available yet. The developer guide covers
[standalone discovery and pairing](../development/AUTOMATIC-PAIRING.md), not
container deployment. The following steps describe Home Assistant OS.

## 1. Install the integration through Marketplace

1. Open **Settings > Marketplace** as an administrator.
2. Accept the community-content notice if this is your first installation.
3. Select **Add from link** in the upper-right corner.
4. Enter this **GitHub link**:

   ```text
   https://github.com/patrickvorgers/open-energy-planner
   ```

5. If prompted, select **Continue**, open GitHub's device-sign-in link, enter
   the displayed code and approve the connection. Return to Home Assistant.
   Do not create a personal access token or paste a GitHub password into HA.
6. Select **Add**. If Marketplace asks for a **Type**, choose **Integration**
   and select **Add** again.
7. Open **Open Energy Planner**, select **Install**, check the release version
   and confirm **Install**. Marketplace downloads the release's integration ZIP.
8. Restart Home Assistant to load the installed integration before starting the
   planner. If an advanced standalone setup form opens, close it. The app below
   establishes the Supervisor connection automatically.

OEP does not need to be in the default Marketplace catalog for this route.
A custom repository is installed and updated through Marketplace after you add it.
Keep the GitHub connection active so Marketplace can check this custom repository.

## 2. Add the app repository and install the planner

The Marketplace does not install apps. Add the planner's repository separately:

1. Open **Settings > Apps > App store**.
2. Open the three dots menu and select **Repositories**.
3. Add this URL, including the branch suffix:

   ```text
   https://github.com/patrickvorgers/open-energy-planner#ha-app-store
   ```

4. Close the repository dialog. Reload the store if the new repository is not
   visible yet. Under **Open Energy Planner**, open the app with the same name.
5. Check that its version matches the integration version installed in step 1.
   Select **Install** and wait for the local image build to finish. The first
   installation downloads the Python runtime and planner dependencies.
6. Leave the app options empty for the default SQLite database. No HA user token
   or manually copied server address is needed. External database options are
   explained in [Data storage](data-storage.md).
7. Enable **Start on boot**. Enable **Show in sidebar** if you want a sidebar
   shortcut. Select **Start**.

The `ha-app-store` branch contains the original tested app build context from
the latest stable release. It is not the development `main` branch. Keep the
repository URL unchanged after installation. Supervisor uses it as part of the
app's identity.

## 3. Connect and open the app

1. Open **Settings > Devices & services**. After the app starts, its authenticated
   Supervisor connection automatically creates the **Open Energy Planner** entry.
   No pairing approval is needed.
2. If the entry has not appeared, confirm the app is running. Allow connection
   to finish, then use [connection troubleshooting](troubleshooting.md).
   Do not enter a made-up address or copy another installation's credentials.
3. Open the app's **Open Web UI** action or its sidebar entry.
4. Check that the connection is ready. Compare the app build shown in the sidebar
   with `release-manifest.json` from the release. In Home Assistant, open the
   **Open Energy Planner** device and check that its integration version matches
   the app version.

Supervisor establishes the authenticated connection. The app interface uses your
existing Home Assistant browser session. There is no general HA access token to
create for this route. The app does not install or overwrite integration files.

## 4. Configure your first plan

Check **Settings > Dashboards > Energy** in Home Assistant first. OEP reuses
those grid, solar, battery and individual-consumer definitions. Then follow
[Set up your first plan](first-plan.md) for the installation wizard, contract,
optional appliances and your first usable plan. You do not need a source checkout,
Python installation, Dev Container or synthetic demonstration.

## Updates and existing installations

Back up Home Assistant and OEP's active database before updating. Marketplace
provides the integration update under **Settings > Updates**. The app store
provides the planner update separately. Read the release notes and update both
to the same release. Stop the planner while changing the pair, restart Home
Assistant to load the new integration, then start the updated app. Verify both
loaded identities before relying on the new plans. Disable independent automatic
updates of either component to avoid temporarily combining releases.

Preserve the existing integration entry, app options, devices, entities and data.
Do not uninstall the integration as an update procedure. For an existing
administrator-managed local app, adding the public app repository creates a
different Supervisor app identity. Do not start a second planner against the same
database. Follow [installation and data-preservation checks](../development/HAOS-ACCEPTANCE.md)
before switching installation channels.

For missing versions, GitHub sign-in problems or Marketplace installation errors,
see the official [Marketplace guide](https://www.home-assistant.io/integrations/marketplace/).
For missing app packages, confirm a stable release has finished publication and
that you are using AMD64 Home Assistant OS.


## Updates and fresh app installations

Home Assistant keeps one Open Energy Planner integration entry. An ordinary
update retains the same virtual device identities. The integration adds or
removes virtual devices as you change the saved app configuration.

If you want to be able to restore the previous installation, back up Home
Assistant and the OEP app before starting a clean reinstall.

A fresh app with a new installation identity reuses the integration entry after
the new connection is verified. Home Assistant clears the previous app's OEP
devices and integration settings, then creates the devices configured in the
new app. Set up the app and check any automations that targeted the previous
virtual devices. HA OS connects automatically. A standalone app uses the usual
pairing approval.
