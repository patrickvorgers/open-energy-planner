# Optional Home Assistant app installation

This guide describes installing a product release on a user-managed Home Assistant
instance. It is not a developer environment or a required personal-VM test gate.
The repository does not provision or update a personal VM. Developers use the
[real Supervisor Docker environment](DEVELOPER-ENVIRONMENT.md), and deliberate
full CI runs its automated lifecycle, ingress, update and model-history checks.

## Install a matched product release

Use the original successful numbered CI/release artifacts described in
[joint release identity](RELEASE-IDENTITY.md). The app and integration must come
from the same artifact set. Never stamp a source overlay with a numbered build.
Developer source builds remain unnumbered and need no transfer bundle.

For a private repository, install the matching integration ZIP through an
administrator-managed workflow and install/build the matching Home Assistant
app repository. HACS does not support private repositories. The app does not
write integration files. Preserve existing app options, the integration entry,
entity/device identities, database and Recorder history when updating.

After installing or updating both components, load the integration through the
normal HA mechanism and start the app. Supervisor establishes the authenticated
connection without copied app tokens or addresses. Open the app through HA
ingress. Review detected Energy sources, measurement direction, timezone and
available history. Complete only missing settings. Do not replace existing Energy
preferences, device IDs or plans to make installation appear successful.

Verify that restarting HA while OEP is stopped does not prevent the next app
start from triggering discovery. Public announcement freshness must not replace
the retained installation ID, TLS identity or peer. See [readiness diagnostics
and manual fallback](AUTOMATIC-PAIRING.md#readiness-diagnostics-and-manual-fallback).
An accepted bootstrap request alone is not proof of completed pairing or setup.
Do not restart HA implicitly during diagnosis or a stopped-only installation.

Check loaded app/integration identities against the expected original artifact
manifest. A match between two arbitrary components is insufficient. See the
identity guide for the authenticated checker and its secure-transport boundary.
That checker verifies an installation. It does not deploy or upgrade Home Assistant.

## Device and lifecycle observations

Use the [virtual device contract](VIRTUAL-DEVICES.md) for requested modes, missing
measurements, pause and recovery. Existing user automations own physical control.
Installation checks do not authorize operating real equipment. Record actual
platform versions and distinguish checks performed from checks not run.

The Docker [interactive scenarios](USER-TEST-ENVIRONMENT.md) provide simulated
meters, battery, EV, boiler and historical inputs without a personal installation.
They use the actual product app entry point.
Supervisor development coverage is not full HAOS hardware coverage. Boot,
storage-device behavior, host-specific kernel/AppArmor behavior and other CPU
architectures require separate evidence when relevant to a product change.

## Application lifecycle logs

The app's **Log** tab shows `Open Energy Planner starting`, `started`, `stopping`
and `stopped` at INFO level. Every application diagnostic includes an explicit
UTC timestamp with milliseconds, for example
`2026-10-21T21:00:00.123Z INFO [open_energy_planner.runtime.diagnostics.logging] Open Energy Planner started`.
`started` confirms application initialization. The HTTP server owns socket binding.
`stopping` marks graceful shutdown and `stopped` follows background-resource cleanup.
Restart therefore produces stop/start events in chronological order. A startup
failure cannot produce `started`. A hard kill, host crash or power loss cannot
write a final stop event. Logs are stdout diagnostics retained by Supervisor or
the container runtime, not another application-managed history database. Routine
HTTP polling does not become INFO access-log noise. Simulation apps use the same
logging and lifecycle implementation.

Check unfiltered logs too: multiline messages and every traceback/stack line must
carry the same timestamp, severity and logger prefix. Soft updates during each solve
must allow valid active results to publish. Newest queued input is calculated next.
A hard-invalid candidate rejected during publication is an INFO discard, not a
planning ERROR. Such a result must never become current. Loss of required inputs remains visibly not ready, and restoring
valid inputs allows planning to recover. Actual storage faults must still be ERROR
with their full traceback. Automated publication-race, recovery, persistence-failure
and formatter regressions cover these distinctions without injecting a fault into
the user test environment.
