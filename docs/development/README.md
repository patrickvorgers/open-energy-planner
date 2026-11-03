# Development

The [planning notifications guide](PLANNING-NOTIFICATIONS.md) describes actionable
app warnings, recovery, authenticated status reads and deduplicated diagnostics.

Start the [real Supervisor developer environment](DEVELOPER-ENVIRONMENT.md) with
`python -m script.supervisor.dev start`. It supplies actual Supervisor/Core, OEP, simulated
devices and recent Energy history without a personal HA OS VM. The Dev Container
uses this same environment. Existing fast CI remains separate from interactive
startup.

The [thermal storage guide](THERMAL-STORAGE.md) explains electric-boiler physics,
declared hot-water demand, joint planning, virtual HA control and preserved storage.

The [EV charging guide](EV-CHARGING.md) describes shared charge points, native
vehicle controls, joint planning, loss assumptions and simulation-only testing.

The [EMS feature reference](EMS-FEATURE-COMPARISON.md) compares documented DAO,
EMHASS and GBBOptimizer capabilities with OEP requirements, including what to
keep, defer or reject and the evidence needed before adoption.

The [installation planning guide](INSTALLATION-PLANNING.md) describes the app-owned
grid/battery wizard, production basis bootstrap, composed multi-inverter model,
independent validation and capability-driven HA entities. Its [live HA test instructions](INSTALLATION-PLANNING.md#test-against-your-own-home-assistant-instance) explain how to connect a real test instance and why a complete Energy dashboard is required.

The [solar forecasting guide](SOLAR-FORECASTING.md) describes pvlib/PVWatts,
Energy-prefilled multi-inverter setup, local shadow calibration and current
production boundaries.

Ruff 0.17.1 uses its default lint rules plus the explicitly agreed D100-D107/D419 missing/empty-docstring rules from the central `pyproject.toml`. These rules also cover tests/browser tests without path exemptions. Existing code and tests have no legacy baseline. `EXE002` is the sole global exception: Windows Docker Desktop bind mounts and build contexts report ordinary Python files as executable. Targeted `TRY004` suppressions retain existing `ValueError` validation contracts. Their reasons are documented at each raise site. The project retains its 100-character formatter width and Python 3.12 target. The Dev Container installs Ruff and its VS Code extension. The editor explicitly uses `/opt/oep-dev/bin/ruff`. The same pinned version runs lint and formatting checks through `script/ci/check_native.py` for explicit local checks and GitHub Actions. The `dev` extra and Ruff's required-version setting enforce the same version outside the container. A future Ruff upgrade is a tested dependency change.

PR revisions run **Quick checks** before any candidate is requested. The shared
`python script/ci/check_native.py --quick` entry point verifies provenance, lint and
formatting without Docker or app dependencies. The requested quality job repeats
these checks before image builds. Its native stage retains full-scope lint and
format checks. Build and review in Draft, then choose Ready for review to request
one candidate automatically. New commits return the PR to Draft. See
[review and CI stages](CI-STAGES.md) for deliberate `ci: run` retries,
batched review corrections and exact-candidate merge policy.

Follow the [code documentation standard](CODE-DOCUMENTATION.md) for required docstrings, comments, domain contracts and documentation review. Disabling a generic docstring-coverage percentage does not remove the obligation to document code properly.

### What the quality job runs

CodeRabbit is the only required automated review service and must cover the latest
PR head. After Quick, signature and CodeQL checks pass, use an independent local
Codex context to review the Draft, batch corrections and retest. Prepare one final
manual CodeRabbit request through the trusted-main read-only preflight. The
150-file limit counts every changed file. Automatic review triggers are disabled
regardless of plan or star count. Follow the [review procedure](CI-STAGES.md#prepare-one-final-coderabbit-review).
Codex Cloud review is optional and starts only on an explicit human
request for that service and PR. Automatic Codex review is disabled for this
repository. A general request to inspect a PR does not authorize a cloud review.
Do not automatically post or repeat Codex review commands after commits. Keep
evaluating findings already posted by either service. The existing Dependabot
exception remains: no mandatory CodeRabbit or Codex Cloud review, with CI,
verified commits and compatibility/regression assessment still required. See
[the contribution policy](../../CONTRIBUTING.md).

| Stage | Checks |
|---|---|
| Provenance | Original/translated documentation provenance against immutable Git objects |
| Container builds | App, native tests, HA framework tests, provisioning and browser runtimes |
| Native quality | Pinned Ruff lint and format checks. Native pytest, including independent solver scenarios, physics/economics validation, contracts, app HTTP tests, failure cases and documentation links |
| Process and solver smoke | Separate app process, authentication, pause/resume, fresh resume plan and independently checked debug planning scenario |
| Home Assistant | Actual HA framework/config-flow tests and authenticated app/integration handoff |
| Provisioning | Fresh onboarding, repeat setup, interrupted-setup recovery and rejection of non-development installations |
| Browser | Existing developer-entry Playwright tests at desktop/mobile sizes |
| Reports and cleanup | JUnit artifact upload plus cleanup of the run's isolated containers/network, including failure paths |

The provisioning smoke test requests its own new validated plan from the isolated
synthetic app before onboarding. It does not rely on commands left by the earlier
HA suite, which can expire at a quarter boundary plus the normal grace period.
It rejects a live source, failed planning, retained older output and an expired
replacement. This fixture preparation never extends production command validity.

These are separate test suites executed by one orchestration entry point, not one undifferentiated test. A single Actions job avoids rebuilding the Docker runtimes in multiple runners. One job per individual test would add overhead without useful isolation.

### Reusing image layers without reusing test results

After native checks and database startup, one background worker runs the real
database contracts while the foreground starts the app and runs HA, provisioning
and browser checks in their existing order. At most two suites overlap in the
same runner: the database suite and one HA/browser stage. Their application stores,
containers and report files are independent. Both paths must finish before the
orchestrator removes its project. Either failure fails the job. The executor joins
its worker even when a foreground check raises. Barrier-based regressions prove
overlap and failure propagation without running Docker in unit tests.

Browser tests launch Chromium once per suite instead of once per case. Every case
still owns a fresh context and page with its original desktop/mobile viewport,
touch and timezone settings. App-setup cases also own a temporary app store.
Context managers close pages and contexts even after failed assertions. A
real-browser regression checks that cookies, local storage and session storage
do not survive context replacement.
The Playwright Python client must match the version of the bundled-browser image.
Update the UI test image, its client pin and the Dev Container client pin together.
A native regression rejects inconsistent versions before browser tests run.

Actions builds the current checked-out merge tree with pinned Docker Buildx/Bake
actions and GitHub's build-layer cache. On the ephemeral Ubuntu runner, Docker's
containerd image store and Docker build driver share layers between builds and
test containers. This avoids keeping a second builder store plus imported image
copies. The HA framework image and ephemeral provisioning service must pin the same HA
version and digest. Otherwise provisioning downloads a second large HA runtime
after the framework build and can exhaust the hosted runner's disk. A native
regression checks this invariant on every full and documentation-only run.
Update both pins together. The persistent real-Supervisor developer installation has
its own upgrade lifecycle and is not upgraded by the ephemeral CI recipe.
Existing daemon settings are retained when enabling the feature. Local Docker
settings are never changed. Optional personal Windows/Linux runners use a
separate Buildx container instead of changing or restarting the host daemon.
The workflow keeps the same test selection and artifacts. See
[personal runner execution](CI-STAGES.md#personal-runner-execution) for owner-only
labels, availability fallback and setup. Separate scopes hold the app, native,
Home Assistant and browser layers. The native recipe builds once with three tags
for the native, provisioning and database suites, avoiding duplicate image exports.
Dependency/base-image layers can survive source changes. Docker still hashes
every build input. A restored layer is not permission to use an older source
tree. Cache misses rebuild normally. Optional cache exports have a 60-second
timeout and cannot turn a build/test failure into success.

Bake loads the images with the current run/attempt's isolated Compose tags.
Its optional build-record upload is disabled. Required JUnit and installation
artifacts remain available.
`script/ci/ci.py --prebuilt` requires that exact project namespace and runs every
selected gate with image rebuilding disabled. Missing images fail the run.
The ordinary local command remains `python script/ci/ci.py`, which builds normally
and creates its own unique project. Both paths retain provenance, Ruff, all four
database backends, HA, onboarding, desktop/mobile tests, artifacts and failure
cleanup. Documentation-only routing and the trusted signature workflow remain
unchanged. No test-result cache, private registry, paid service or secrets are
added. Caches contain development images/source and use normal repository access
and branch cache isolation. No production configuration enters the build context.

Each Compose operation logs its elapsed seconds, including failed operations.
Compare cold and warm hosted runs of the **same commit**. Include builder setup,
cache transfer and post-job export in the job duration. A first run populates
the cache and can be slower. Source/dependency changes, cache eviction, hosted
runner variance and cache service availability affect the result. Runner time,
queue time and billed minutes are distinct: sum job durations (rounding each job
up to minutes) to estimate allowance consumption. Do not claim an account billing
measurement from workflow elapsed time alone.

### Documentation-only PRs

The same **Quality and integration (AMD64)** status uses focused checks only when
every changed path is a regular Markdown file under `docs/` or `design/`, or one
of root `README.md`, `CONTRIBUTING.md` and `AGENTS.md`. Additions, edits, deletions
and moves within these paths qualify. Executables, symlinks and gitlinks do not.
Other paths follow the [change-based matrix](CI-STAGES.md#change-based-candidate-matrix).
Unknown root documents, legal/package notices and unclassified inputs remain full.

CI reads `script/ci/ci_scope.py` from the immutable PR base revision and compares
that base tree with the checked-out synthetic merge commit. This checks the
complete PR against current base content, including runtime changes in earlier
commits and both sides of renames. A changed classifier cannot grant itself a
shorter route. Empty diffs, missing history/policy and unreadable data select full
checks. Main
pushes reuse only an identical successful full candidate through the [checked reuse gate](CI-STAGES.md#reuse-an-identical-tested-merge-on-main). Manual runs always run the full suite. This routing is a quality
control, not an adversarial boundary against same-repository workflow writers.

Focused runs build only the existing pinned native runtime. They retain:

- Ruff lint and format checks over all maintained Python code.
- Immutable Git provenance verification and its regression tests.
- Requirements inventory, local documentation links and database dictionary
  table/column coverage tests. Meaning, completeness and examples remain review
  obligations. Coverage does not prove semantic accuracy.
- Scope-classification and orchestration regressions, including failure cleanup.
- The separate trusted commit-verification job and latest-head CodeRabbit review.

They omit app/database/HA/provisioning/browser runtimes, runtime scenarios and
installation-bundle generation because eligible edits do not change their inputs.
JUnit documentation results are uploaded as usual. Skipped workflow steps are
visible inside the same quality job. No required status is skipped by a workflow
path filter. A failure in any retained check fails the job.

Run `python script/ci/ci.py --documentation-only` locally to repeat these gates.
This explicit local flag does not prove that a PR qualifies. Actions determines
eligibility independently. Infrastructure changes still
require `python script/ci/ci.py`. No application or HA configuration is changed by
either CI route.

### CodeRabbit tool ownership

Project CI owns pinned Python linting and formatting. CodeRabbit's Ruff and Flake8 integrations are disabled to avoid duplicate Python-lint findings and provider version mismatches. Its blanket docstring-coverage pre-merge check is disabled because that percentage is not a project requirement. Useful documentation still receives review.

Pylint, security/secrets scanning, workflow/container/YAML analysis and GitHub-check integration keep their provider defaults: they can add findings beyond our executable checks. GitHub-check integration consumes CI evidence rather than rerunning our test suites. No blanket tool shutdown, review-profile reduction or measured provider speedup is implied. Provider settings are validated against the published CodeRabbit schema when edited. Actual activation/performance must be checked on a subsequent provider run.

## First start

Install Git, Docker with Compose, VS Code and the Dev Containers extension. Clone the private repository using your GitHub account, open its root in VS Code, and choose **Dev Containers: Reopen in Container**.

The optional static [developer start page](../developer/index.html) offers the browser-to-VS-Code entry pattern used by HA/DAO: enter a repository/branch URL and open the Dev Container clone flow. A direct [Open in Dev Containers link](https://vscode.dev/redirect?url=vscode%3A%2F%2Fms-vscode-remote.remote-containers%2FcloneInVolume%3Furl%3Dhttps%253A%252F%252Fgithub.com%252Fpatrickvorgers%252Fopen-energy-planner) also needs no hosted page. Browser/VS Code clone/trust prompts remain the developer's responsibility. The successful CLI receipt covers a fresh ordinary checkout, not the graphical container-volume flow.

To view the page locally, run `python -m http.server 8765 --bind 127.0.0.1 --directory docs/developer`, then open `http://127.0.0.1:8765`. To publish later, select GitHub Pages source **GitHub Actions** and set repository variable `DEVELOPER_PAGES_ENABLED=true`. Intended URL: `https://patrickvorgers.github.io/open-energy-planner/developer/`. Hosting requires an eligible plan or public repository and is currently disabled. Only three public static files are published, never design/measurement archives or developer credentials. The page launches local installation. It does not run provisioning on GitHub.

The Dev Container starts the real Supervisor environment, installs the normal OEP app and integration, completes HA onboarding and loads simulated devices and history. Provisioning fills only missing sample settings and preserves existing settings, partial configuration and HA identities. No production HA settings are copied. See the [developer environment guide](DEVELOPER-ENVIRONMENT.md) for the host-only start command and source-update workflow.

Open **http://localhost:7123/app/local_open_energy_planner**. Run `python -m script.supervisor.dev start` from a host terminal to provision the environment and copy the generated HA account to `.local/dev/developer.json`. This ignored file contains private credentials and a refresh token; do not share it. OEP ingress needs no API key, pairing code or separate app login. The Installation wizard is inside the app. All commands affect virtual entities only.

The privileged developer container owns a private inner Docker engine. The host Docker socket and private signing keys are not mounted. Image builds and normal market/weather adapters require internet access; the Docker bridge is not an egress firewall. Stop the development stack from the host with `python -m script.supervisor.dev stop`. This preserves all volumes and data.

## Commands

- `python script/ci/setup.py`: inside the Dev Container, repeat native/UI quality checks. Actual Supervisor provisioning uses `python -m script.supervisor.provision_supervisor` inside that container.
- `docker compose --profile test build ui-tests` and `docker compose run --rm ui-tests`: Chromium checks at desktop/mobile sizes, without launching VS Code. Full CI includes these tests.
- `python script/ci/ci.py`: from a host with Docker, run the same full isolated suite as GitHub Actions. It builds the app and both test runtimes, runs Ruff/format/native scenarios/smoke and actual HA handoff, then removes only its uniquely named test project.
- `python -m script.supervisor.dev start`: from the host, repeat onboarding and stage current sources while preserving existing state. Do not run the host launcher inside the Dev Container.
- After editing app or integration sources, repeat the host start command. The provisioner rebuilds the changed local app and reloads the changed integration; unchanged source is reused.

CI writes fresh JUnit files to ignored `results/ci/`. The HA suite uses its own framework runtime and must not be skipped silently in the full pipeline. Native tests run without a network. No tests require production credentials or physical equipment.

VS Code supplies named setup/native/browser tasks. Press **F5** with **Open Energy Planner: debug synthetic planning round** to inspect the real synthetic model and independent validation directly. This development-only launcher avoids the app worker's parent watchdog terminating a paused debugger. It publishes no commands and changes no production timing. The command is tested. Interactive breakpoint stopping is not claimed as verified. The separate app process smoke still tests worker/HTTP behaviour.

## Repository and release boundaries

`src/` contains the app/recalculation code. `custom_components/` contains the thin HA integration. `design/` contains the agreed requirements and design. The [solver selection decision](../../design/SOLVER-SELECTION.md) records the selected stack. GitHub Actions publishes test artifacts only. It does not publish containers, HACS or Supervisor releases. The product UI provides Installation and Contract settings, Planning, Savings, Forecast quality, appliance configuration/control and EV charging. Desktop/mobile browser checks exercise these flows against app APIs and fixture data. Their scope does not establish physical hardware execution.

Use signed commits and pull requests as described in [CONTRIBUTING.md](../../CONTRIBUTING.md). CodeRabbit needs a separately enabled GitHub App installation. The YAML file alone does not activate it. Repository protection also depends on the account's support for private-repository protection. Record any unavailable external setting explicitly rather than claiming it is enforced.

## Measurement source development

See [demand forecasting](DEMAND-FORECASTING.md) for the always-active startup model,
historical residual demand, local Ridge candidate, app-owned source setup and
the decision-benefit activation boundary.

See the [history adapter](HA-HISTORY.md)
for optional Recorder API reads, typed hourly energy, automatic missing bonus
baselines, manual recovery and actual-HA acceptance coverage.

See the [HA measurement-source boundary](HA-MEASUREMENT-SOURCES.md) for app-owned source selection, observation forwarding, snapshot assembly, supported units, tests and production input readiness.

## GitHub-verified PR commits

See [trusted signature verification](VERIFIED-COMMITS.md) for the base-only target workflow, commit/status checks, permissions and trusted-base activation. The offline pytest suite tests acceptance/failure behaviour. It never requires a GitHub token or network access. The trusted network gate runs separately in GitHub Actions.

## Native installation and virtual devices

Use the [real Supervisor developer environment](DEVELOPER-ENVIRONMENT.md) and its automated acceptance for development. The optional [Home Assistant installation guide](HAOS-ACCEPTANCE.md) covers product artifacts. The [device contract](VIRTUAL-DEVICES.md) defines modes, capability permissions and numeric applicability.

## Durable plan archive

See [plan archive](PLAN-ARCHIVE.md) for SQLAlchemy-backed immutable plans, shared input blocks, database deployment, the pre-release no-migration decision and consistent backup procedures.
