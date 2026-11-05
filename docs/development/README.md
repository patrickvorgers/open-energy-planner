# Develop OEP

Start here if you want to change Open Energy Planner. For installation settings and daily use, follow the [user guide](../index.md). This page gets a contributor to the right environment, source owner and validation route. The [reference library](REFERENCE-INDEX.md) contains the detailed contracts.

## Choose the environment for the task

| Task | Environment | Start with |
|---|---|---|
| Understand a user-visible plan | Local synthetic app, no HA connection | [First plan](../users/first-plan.md) |
| Edit prose or inspect code | Ordinary checkout and the relevant local dependencies | [Documentation preview](DOCUMENTATION-SITE.md#local-build-and-preview) or the subsystem reference |
| Change a pure calculation or app behavior | Focused tests with the pinned native dependencies | [Local checks](#run-the-smallest-sufficient-checks) |
| Exercise actual HA, pairing, ingress and lifecycle | Isolated real Supervisor developer environment with an accepted numbered package | [First start](#first-start) |
| Inspect populated appliance, EV, boiler and history behavior | Simulated devices in an isolated developer environment | [Interactive scenarios](USER-TEST-ENVIRONMENT.md) |

Production Home Assistant is not a development dependency. A local model test, real HA framework test, Supervisor acceptance and physical-device test establish different things.

## First start

Install Git, Python 3.12 or later, Docker with Linux containers, VS Code and its Dev Containers extension. The public [provisioning page](https://patrickvorgers.github.io/open-energy-planner/provisioning/) opens the normal VS Code clone flow and explains host preparation. It does not run Docker on GitHub.

1. Clone the public repository into a local folder. Check `python --version` in the host terminal used to launch VS Code.
2. Obtain the **original** `installation-bundle-amd64` ZIP from a successful candidate whose tested tree matches a GitHub Verified merge on `main`.
3. Use a clean checkout of that actual merge. Do not prepare a persistent installation from an edited or unmerged source overlay.
4. Accept the package and start from the **host** terminal at the repository root:

   ```console
   python -m script.supervisor.dev prepare --bundle /path/to/installation-bundle-amd64.zip --run-id RUN_ID
   python -m script.supervisor.dev start
   ```

5. Open `http://localhost:7123/app/local_open_energy_planner`. The ignored `.local/dev/developer.json` holds the generated private HA account. Ingress authenticates the app without copying an administration token.
6. Choose **Dev Containers: Reopen in Container** in VS Code when you need the pinned editor/tool environment.

The run ID is the original candidate run, not a later main run that reused it. GitHub read access comes from the existing credential manager or the documented host token environment. Never enter credentials on the public start page or copy signing keys into the container.

The host terminal and VS Code must resolve `python` to a supported version. On Debian/Ubuntu, install `python-is-python3` only when `python3 --version` is already supported. Alternatively, activate a host virtual environment and launch a new VS Code process from that terminal. A shell alias alone does not configure the Dev Container's host initialization command.

Package verification, update, cache recovery and startup ownership are specified once in the [Supervisor environment guide](DEVELOPER-ENVIRONMENT.md#start-and-stop). Stop and status are host commands and preserve existing volumes:

```console
python -m script.supervisor.dev status
python -m script.supervisor.dev stop
```

## Keep editing separate from persistent package acceptance

Persistent startup validates the accepted package and tracked checkout. Even documentation or test edits can block reopening that package checkout. Keep an editing branch/worktree for changes and a clean verified merge checkout for the numbered installation. Do not discard work or fabricate build identities to bypass the check.

Use focused tests or disposable Supervisor acceptance for unmerged source changes. Update the persistent environment only from the original matching successful CI bundle after merge. Provisioning preserves existing settings and HA identities and supplies only missing simulated examples. Do not reset retained data to make a screenshot or test match an example.

## Find the responsible code and contract

Read the [architecture overview](ARCHITECTURE-OVERVIEW.md) before changing a boundary. App calculations belong in the app. The HA integration stays thin. Physical equipment actions remain in user-owned automations.

The [reference library](REFERENCE-INDEX.md) groups every maintained subsystem guide by contribution task, including forecasts, prices, contracts, appliances, storage, UI and release proof. Formal requirements and proposed design remain distinguishable from implemented behavior. Preserve independent numerical scenarios instead of changing their answers to agree with the solver.

## Run the smallest sufficient checks

During development, run Quick and the affected regressions before a tested push:

```console
python script/ci/check_native.py --quick
```

Use the existing test environment for the affected group. Browser cases run in Chromium at desktop/mobile sizes. App-side HA wire contracts do not start HA, while Recorder/platform contracts require the actual HA runtime. Every database change retains the real SQLite/PostgreSQL/MariaDB/MySQL matrix. Installation and lifecycle changes retain their provisioning/Supervisor coverage.

The shared commands and exact suite ownership are in [CI stages](CI-STAGES.md#change-based-candidate-matrix). Avoid running the complete local suite as a duplicate prerequisite for every PR. The complete required suite union runs once through the admitted GitHub candidate. A diagnostic full local run needs a concrete reason and an actual result in the PR.

For documentation, use the [strict local site build and link checks](DOCUMENTATION-SITE.md#local-build-and-preview). A successful build validates rendering, not the truth or usability of an instruction. Read the result as a new user or contributor.

For interactive model debugging, VS Code provides **Open Energy Planner: debug synthetic planning round**. It exercises the synthetic model and independent validation without publishing physical commands. Interactive breakpoint behavior is separate from automated worker/HTTP evidence.

## Prepare a contribution

Follow [CONTRIBUTING.md](../../CONTRIBUTING.md) for signed public commit identity, Draft preparation and documentation impact. The [review and CI stages](CI-STAGES.md) own provider selection, current-head review, finding disposition, queue coordination, Ready admission and exact-tree merge proof.

Do not treat a green skipped review, an older candidate or another owner's local result as current merge evidence. Preserve an active candidate's agreed base. Changes to the head require fresh review and the appropriate Ready transition.

## Know what a successful change proves

Local checks, hosted candidate CI, a Verified exact-tree merge, main checks and a loaded installation are separate evidence. A focused candidate proves its selected suites. It is not full-suite, physical-device or deployment evidence.

GitHub Actions produces original test reports and matched app/integration bundles. It does not silently install OEP into a user's Home Assistant. Use [joint release identity](RELEASE-IDENTITY.md) and [the release scope](../../design/FIRST-RELEASE-SCOPE.md) before making a deployment or support claim.
