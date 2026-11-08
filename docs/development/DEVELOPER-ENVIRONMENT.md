# Real Supervisor developer environment

`oep-dev` is the local AMD64 development environment. It runs the official Home
Assistant apps development image with its own Docker engine, actual Supervisor,
Core, the normal OEP product app and the real integration. No personal HA OS
machine, address or credential is required.

## Start and stop

Install Python 3 and Docker with Linux containers. Persistent `oep-dev` runs only
numbered installation bundles from successful Checks runs whose PR has been
merged onto `main`. Download the original `installation-bundle-amd64` ZIP from
that Actions run. Keep the ZIP intact: downloading its individual files and
rezipping them changes the GitHub artifact digest.

Use a clean checkout of the actual verified merge commit, then run from a host
terminal at the repository root:

```console
python -m script.supervisor.dev update --bundle /path/to/installation-bundle-amd64.zip --run-id 40969348570
```

The run ID identifies the original successful candidate, not the later main run
that may reuse its evidence. The example ID illustrates the argument format:
choose the run whose merged source you intend to install. GitHub read authority
comes from `GH_TOKEN`, `GITHUB_TOKEN` or the existing Git credential manager.
Credentials are never saved in the bundle receipt. No GitHub write access is
needed. Local source packages, unmerged PRs, Quick-only runs, expired artifacts,
failed or skipped required suites and artifacts from another attempt are rejected.
Full-suite runs require successful full-tree validation. Focused runs intentionally
skip that full-only marker, but still require successful source capture, selected
suites, package creation and artifact upload. Failed or unfinished marker steps
are rejected on both routes. The original artifact digest and manifest source
commit must still identify the actual merged tree.
The package's tested Git tree must equal the actual GitHub Verified merge tree
and that merge must still be on `main`. An older accepted merge can be selected
explicitly. The command does not automatically select or roll back versions.

After accepting a package, these commands reuse it without contacting GitHub:

```console
python -m script.supervisor.dev start
python -m script.supervisor.dev status
python -m script.supervisor.dev stop
```

The first start builds the developer tools, stages the accepted app/integration pair,
starts Supervisor and installs OEP as a local app. It completes HA onboarding,
loads the simulated Energy devices and their recent history, waits for automatic
Supervisor pairing, then fills only missing sample installation/contract settings.
Existing settings and partial configuration are preserved. Warm starts reuse
unchanged packaged sources instead of rebuilding them. Package updates reload
the integration and rebuild the local app. Stop retains all development volumes.
`start` and `prepare` have no source-copy fallback. A missing or altered package
stops them before Compose starts or updates the environment. Changes to or removal
of any recorded tracked checkout file also block them, including documentation
and tests. Added files under `script`, `test_environment`, `src` or `.devcontainer`
also block them, except Python bytecode caches. Stop and status need no bundle.

The accepted original archive, provenance receipt and extracted components live
under `.local/dev/bundle`. `.local/dev/developer.json` and persistent volumes
remain separate. Before replacing the cache, all archive paths, payload hashes,
paired versions and build identities are checked. A staging rename failure
restores the previous cache. During provisioning the cache and controller sources
are checked again. Readiness requires `/v1/version` to report both loaded
components matching the package version/build, plus the app's source revision.
The success marker is written only after this check. A failed runtime check
does not automatically roll back databases or claim a successful update.
Process interruption between the cache directory renames can leave no active
cache. Restage the original CI archive to recover. Persistent volumes and
credentials remain separate from that cache.

`Build` displays the original Actions run number and attempt, for example
`733.1`. An untagged CI package may still display `Development` as its release
version. Its numeric build remains visible. The app tarball and integration ZIP
both contain the stamped `build-info.json`. The repository templates remain
unnumbered and are not deployable persistent packages.

Open `http://localhost:7123/app/local_open_energy_planner`. Use the
generated Home Assistant account recorded in `.local/dev/developer.json`.
The password and refresh token remain private local files. They are not printed
or packaged. OEP ingress needs no administration token, pairing code or separate
app login.

VS Code can reopen the repository in its Dev Container. The initialization task
validates the previously accepted package on the host, including Windows Git
worktrees. Prepare it first using the host `prepare` action with the same
`--bundle <zip> --run-id <id>` arguments. The start task then uses the same
Supervisor provisioner. Use the host signing setup for commits.
Never copy private signing keys into a container.

After edits, run focused tests or disposable Supervisor acceptance. Update the
persistent lab only after merging and obtaining its successful CI package.
Do not run the host launcher inside the Dev Container.
It explicitly rejects that invocation rather than creating another nested stack.

## History and model evidence

The existing simulated fixture creates recent energy-meter statistics and keeps
their stable device/entity identities on restart. It provides approximately
32 days of recent history. History initializes only the isolated lab. OEP retains
the normal solver, storage, market/weather adapters and model-selection policy.
The sample contract and measurements are simulation, not supplier billing or
physical equipment evidence. The actual product entry point is unchanged.

### Ready-to-use appliance examples

After automatic pairing and foundation setup, provisioning uses the normal app
APIs to select the simulated heat pump as a weather-sensitive forecast consumer
and configure the dishwasher, washing machine and dryer as shiftable consumers.
Their Energy power links come from the same fixture. Bootstrap supplies explicitly
simulated five-minute inputs to the normal automatic detector, rather than inserting
learned profiles. Dishwasher and washing machine have two complete cycles, yielding
105/135-minute detected active spans and 0.83/0.92 kWh. The simulator's final idle
quarter is excluded. Dryer has one cycle plus a manual 180-minute, 1.40-kWh fallback.
The selected heat-pump source has no Energy power link and demonstrates that message.
These bootstrap inputs are not claimed to be prior Recorder measurements, historical
predictions or model-quality results. See [automatic appliance learning](APPLIANCE-LEARNING.md).
No planning request is submitted automatically.

Two cars and two shared charge points are configured, with usable capacities of
60/40 kWh and respective AC limits of 11/7.4 kW. Both cars are available at each
point. The minimum is 1.38 kW. Existing connection state is retained and a user
must explicitly identify the connected car. A 200-litre, 2 kW electric boiler
uses a 45-65 degree comfort range and the simulator's morning/evening draw.
Six instances of the shipped appliance, EV and thermal blueprints bind only to
simulator actions. Physical equipment is never selected.

Open Energy Planner is enabled in the HA sidebar once through Supervisor's
native panel option. Hiding it afterward is respected. The private development
journal at `/mnt/supervisor/oep-dev/example-provisioning.json` records completed
bootstrap steps and interrupted profile writes. It is not product configuration.
Warm provisioning does not recreate removed examples, overwrite edited settings
or submit tasks. A one-time learning update recognizes only the exact former
synthetic bootstrap profiles, with no open task or manual edits. Other existing
profiles stay untouched. Existing nonempty device sections and selected
consumers remain user-owned. Partial foundation setup is left to the wizard.
An attempted configuration write does not establish ownership. Recovery must
match the intended simulator identities, source bindings, limits and intent
before creating dependent automations. Intervening user edits remain untouched.
The HA phase rereads current consumer selection, appliance shiftability, EV and
thermal configuration before constructing pending automations, including while
waiting for virtual entities. Removed or deselected appliances are skipped. A past
ownership receipt alone cannot authorize new fixed simulator actions after
source, limit or intent changes. Existing automation instances remain untouched.
An unconfirmed write followed by an empty result stays empty. Setup cannot
distinguish a lost request from a subsequent user removal, so it does not
automatically recreate that example, automation or sidebar setting. Matching
saved objects still resume without duplicate writes.
For the sidebar, only known pre-dispatch rejections or failures before sending
allow a corrected retry. Supervisor `unknown_error` responses can represent a
timeout after applying the option, so they remain ambiguous like unexpected
response IDs or a lost connection during dispatch.
Fixture discovery waits for delayed Energy consumers for up to 60 polls, two
seconds apart. An explicit setup refresh requests missing Energy metadata.
Energy membership remains authoritative for the example consumption meters.
Unconfigured car and boiler sensors are obtained through separate EV and thermal
source-choice requests. These choices do not enlarge the active catalogue until
their configuration is saved. Existing pending queries and usable results are
reused. The server expires a pending choice request 60 seconds after its creation
and reports it as failed. A reused pending request may therefore have less time
remaining. The script's separate outer guard allows at most 60 polls, two seconds
apart, but stops when the server reports failure or expiry. Failed, incomplete or
timed-out results stop provisioning before example settings are written. Check
the HA connection and simulator sources before retrying provisioning.

Choices are checked again immediately before each version-guarded device save,
so a result that expired during profile learning is refreshed first. Completed
journal sections and existing user device settings need no new chooser request.
After resolving a source or connection problem, run the normal developer start
again. The journal resumes accepted work while preserving user edits and removed
examples.

The contract uses the documented 2026 Zonneplan example, including the daylight
export bonus, positive-price condition and 7,500 kWh annual cap. Its initial
contract date is 28 HA-local days before provisioning, within the fixture's
available export history. Only an absent contract or the exact unchanged former
default is initialized. Custom rules and subsequent changes are preserved.
See [the test guide](USER-TEST-ENVIRONMENT.md) for formulas and accounting limits.

Training history is distinct from archived predictions and realized decision
windows. Seeding measurements does not invent past forecasting knowledge or
prove model accuracy. Chronological evaluation must use only inputs available at
each decision time. The existing replay and model-selection tests retain their
independent numerical expectations.

## Automated Supervisor acceptance

Run `python -m script.supervisor.check_supervisor` from the host to exercise a fresh actual
Supervisor without changing `oep-dev` or another persistent lab. Each invocation
owns a unique Docker project and new private inner-engine volumes. It removes
only those temporary resources afterward. No host Docker socket is mounted.

Acceptance checks clean onboarding, automatic pairing, a real history-trained
household baseline and individual shadow models, desktop/mobile HA ingress, unchanged warm provisioning, outer
stop/start and an actual Supervisor app update. The local update uses an
explicit test-only development version with no numbered build identity. Its app
and integration identify the same actual source revision. Every application
table must remain exactly equal while stopped across the update. Saved settings,
HA identities and original plans must survive when the app resumes.

Provisioning reuses local store metadata when the app is installed and its source
digest is unchanged. A missing app or changed app source still requires a store
reload before installation or rebuilding. Integration-only changes retain their
Core restart without refreshing unrelated app stores. Registry and required
reload failures remain fatal. CLI failures retain up to 4,096 credential-redacted
stdout/stderr characters so a failed operation can be diagnosed without another
blind run.

Only disposable Supervisor acceptance (`OEP_SUPERVISOR_SMOKE=1`) reloads the
store through a direct POST with a 180-second request timeout. The pinned CLI
otherwise stops waiting after 30 seconds. Before reading its own CLI container's
token, the test verifies ownership of the private Docker engine and the inner
Supervisor container's fixed local network address. The token stays in memory.
Proxies and redirects are disabled. Responses are limited to 64 KiB and must
report both HTTP and Supervisor success. Failure stops acceptance without retry
or skipped checks. Other commands and environments retain the normal CLI.

The trained heat-pump and dishwasher ridge models must remain in shadow without
realized decision evidence. The heat-pump model must use weather features.
The ridge models still require at least 28 days of actual hourly training data.
Only the disposable candidate-source app uses a test bootstrap with the existing
offline temperature fixture. It covers 32 historical days and three future days,
with receipt time retained as `known_at`. These temperatures are simulated.
Real Recorder imports and production Ridge fitting, prediction and validation
remain in use. Acceptance waits for trained Ridge identities, heat-pump weather
features and the completed missing-laundry-history result before checking them.
Timeout diagnostics identify each expected model and its training/weather state.

The normal Supervisor bootstrap still owns configuration, authentication,
privilege drop and runtime startup. Release packaging and numbered installation
bundles never contain the test bootstrap or weather fixture. Persistent `oep-dev`
continues to use the accepted production bundle and ordinary weather providers.
The offline fixture prevents external Open-Meteo availability from deciding
Supervisor lifecycle acceptance. Separate demand-weather adapter and consumer
tests retain HTTP/schema failures, coverage validation and calendar fallback.

The separate annual startup scale uses all available common complete days within
the last 365 days. The 32-day fixture is no longer restricted to a recent 28-day sample.
A just-completed midnight reference can still be awaiting Recorder compilation.
Acceptance checks the shared cumulative-sum reference period
without treating the provisional scale as proof of machine-learning training.
New washing-machine and dryer meters do not have seven complete days of history.
Their absence must keep the full composition unavailable and the history-trained
household baseline active. Synthetic cycle profiles are not forecasting history.
The existing independent chronological replay cases run unchanged. They prove
that future-issued forecasts cannot become historical knowledge, three measured
0.40 gains permit promotion and a later 0.10 loss restores the baseline. Seeded
history alone does not establish economic benefit or authorize promotion.

The update acceptance waits for Supervisor to report the app stopped before taking its database
baseline and again before comparing every table after updating. A successful stop response alone
does not confirm the cached Docker-event state has settled. The existing app-readiness timeout
bounds both checks. A missing stopped state fails acceptance before reading a live writer.

`python script/ci/ci.py` includes this slower acceptance after the fast suites and
their temporary database services stop. Deliberate full candidate and release CI
therefore require it. Quick development checks and documentation-only CI do not.
`results/ci/supervisor-timings.json` records image/engine setup, cold provisioning,
warm provisioning, restart and update separately. The replay JUnit report and
desktop/mobile ingress screenshots accompany CI reports.

## Isolation and retained data

Bootstrap identifies the outer lifecycle using the kernel boot, init-process start
time and network namespace. A reused namespace number cannot retain managed
containers in an earlier network context. Repeated provisioning within the same
lifecycle keeps running containers. Rebinding replaces only Supervisor-owned
containers and preserves their volumes, settings and history.

Compose owns only project `oep-dev` and its three named volumes. The host
launcher requires a local Unix socket or Windows named pipe before staging
sources or starting services. Explicit TCP and SSH Docker transports are rejected.
The checked endpoint is pinned for this command's Docker children, even if another
process changes the active Docker context.
The privileged development container has its own Docker socket and engine. The host Docker
socket is never mounted. Provisioning checks the container markers, mounted
Supervisor volume and Docker engine identity before changing anything. A host or
remote engine is rejected. Only loopback UI/observer ports are published.

The provisioner replaces only the app build context and custom-integration source
directories. It does not replace app data, Recorder files, saved configuration,
automation or credentials. Public source permissions remain readable when the
product entry point drops root. Generated source staging remains under
`.local/dev`, outside release inputs. No numbered installation identity is invented.

`python -m script.supervisor.check_supervisor` retains a separate candidate-source route for
CI acceptance before merge. It stages `.local/dev/test-app`, creates a unique
disposable project and passes its explicit test marker to the provisioner. It
does not replace `.local/dev/bundle` or update the persistent `oep-dev` project.
Ordinary `script.supervisor.dev` commands never enable that route. This is an operational
guard against accidental source deployment, not isolation from an owner who can
edit or bypass the repository's launcher.

Existing `oep-user-test` volumes are not renamed or silently adopted. They remain
separate while the replacement environment and its lifecycle checks are proven.
No personal VM deployment is part of this workflow. General product releases and
their matching app/integration artifacts remain available for user installation.

Offline price/weather fixtures remain test-owned with independent numerical expectations. Existing lab volumes are not removed or automatically migrated by developer setup.

## Platform boundary

The developer image and Supervisor image are pinned by digest. The provisioner
checks the supported Supervisor/Core versions and disables Supervisor automatic
updates. It never silently upgrades or downgrades an existing development Core.
Review platform pins together when updating HA compatibility.

This is a Supervisor development system, not a full HAOS host. Supervisor may
report `supported: false` for this expected development host. Require actual
health and functioning product APIs. Hardware, host-specific AppArmor behavior,
other CPU architectures and physical device actions are not established here.
The existing fast test suite remains available through `python script/ci/ci.py`.

The provisioner disables Supervisor automatic updates in its retained updater
configuration before creating the Supervisor container. This prevents first-boot
update scheduling from racing the later API option. Other updater settings remain
unchanged. The expected Supervisor/Core versions are still checked before setup
continues. A newer upstream release does not authorize a platform upgrade.

### Stable platform updates

The first `FROM` lines in `Dockerfile.supervisor` and `Dockerfile.chain-test`
own the official Supervisor and Core image tags and SHA256 digests. Dependabot's
Docker updater reads these standard root Dockerfiles. The provisioner reads those
same entries and derives the expected API versions, with no separate Python
version list. `Dockerfile.supervisor` declares the unmodified official runtime
image. The Core framework Dockerfile extends the exact runtime image for tests.

Fresh private engines preload the pinned Core digest and configure Supervisor's
supported `override_image` option. Supervisor owns the actual Core container and
APIs. After startup, both running container image IDs must match the digest-pulled
Core and Supervisor images. Readiness first awaits the loaded Hassio config entry,
including when OEP is already running, then rechecks image identities before
publishing the source marker. Equal version strings alone are insufficient. Existing
Core configuration is preserved rather than automatically upgraded. A retained
image mismatch fails explicitly and requires a reviewed platform alignment.

Dependabot checks Docker dependencies weekly and groups Core/Supervisor proposals
in a platform update PR. It updates references to those images together. Only
stable calendar-release tags and complete digests are accepted by the platform
reader. Development, beta, release-candidate and mutable tags fail validation.
Every platform proposal requires full native/database/browser checks and actual
Supervisor lifecycle, ingress, update, retention and replay acceptance. Existing
review/signing gates and the Dependabot review exception remain unchanged.

Core requests a Supervisor update during first onboarding even when scheduled
updates are disabled. Select the newest official stable releases together and
validate the exact combination. Never ignore Supervisor job conditions or modify
Core to make an obsolete pin pass. CI does not deploy updates to persistent labs.
