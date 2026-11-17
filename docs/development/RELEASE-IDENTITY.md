# Joint release identity

The app and integration form one release. The user creates and publishes a GitHub
release. Its tag determines the semantic version, for example `v0.3.0` becomes
`0.3.0`. The title and release notes do not determine the number. Tags may omit
the v prefix and may contain semantic prerelease identifiers. Tags with `+`
build metadata are rejected because HA uses the version as a Docker image tag.
The version after removing the optional v prefix must not exceed 128 characters.
CI never increments a release number automatically.

The Checks workflow runs the full suite for a published release. It stamps the
tag's version into both HA manifests and both packaged identity files without
editing repository sources. Packaging verifies that the tag identifies the
actual checked-out commit. A release-only job attaches the tested app repository,
integration ZIP, manifest and hashes to that existing GitHub release. It cannot
run for pull requests and does not replace existing release assets on retries.
The release tag must include this workflow. Tags on older code cannot gain a
workflow that did not exist at that commit. Publication starts the build. Assets
appear only after checks pass. No deployment to HA is automatic.

## Marketplace and app-store publication

Home Assistant 2026.11 or newer uses its built-in Marketplace. The repository's
`hacs.json` retains that required filename and selects the release asset
`open_energy_planner.zip`. Its minimum HA version is `2026.11.0`. Marketplace
installs only the integration, never the planner app. The
[user installation guide](../users/install.md) owns the two installation routes.

After every asset has been uploaded or verified, `publish_release` publishes
stable releases to the `ha-app-store` branch in this same repository. Users add
`https://github.com/patrickvorgers/open-energy-planner#ha-app-store` to the
Supervisor app store. No extra repository, image registry or access credential
is required. Supervisor builds the original packaged Docker context locally.
Pre-releases keep their downloadable assets but never advance this stable channel.

The publisher checks the archive hash and matched app identity before writing.
It copies `repository.json`, the complete packaged `open-energy-planner` directory
and the original release manifest without rebuilding or restamping them. The
branch starts from the first published source revision. Its original source
scaffold and workflow files remain unchanged. The source `ha_app/config.yaml`
is removed so Supervisor sees only the generated app, whose Docker build context
contains exactly the tested package files. Later publications replace that
directory and remove obsolete app files.

GitHub's `createCommitOnBranch` signs generated publication commits using the
existing workflow identity. This branch is a generated distribution channel,
not an implementation PR or a replacement for `main`. Publication checks GitHub's
Verified result and the full intended remote file identities after writing.
The expected branch head guards concurrent publishers. Retrying the identical
bundle makes no new commit and rechecks its signature and files. A different
bundle for the same version or an older stable version fails. Existing release
assets remain unchanged if app publication fails. Rerun the failed publication
job to resume from the original retained bundle. Never force-push this channel
or replace tested assets to make a retry succeed.

Merging this distribution support does not create a release or install OEP on
any Home Assistant instance. The first stable release must finish full release
CI and publication before either user installation channel is advertised as
available. Integration and app updates remain separate HA actions. Users must
keep both components on the same release and preserve their data when changing
from a local administrator-managed app to the public repository identity.

## Bundle contents and recovery

The app repository includes `icon.png` and `logo.png` beside `config.yaml` so
Home Assistant can display its artwork in Apps. Both files reuse the integration's
canonical `custom_components/open_energy_planner/brand/icon.png`. The package
builder validates this input before copying it and includes both images in the
checksummed app archive. Reload the Supervisor App Store after installing artwork
and verify that the app reports `icon: true` and `logo: true`.

Publication uploads the manifest first. A retry downloads that manifest's original
tested attempt and verifies every existing asset before uploading missing files.
Before any asset exists, publication uses the successful quality job's build
attempt, including a publish-only retry that does not rerun the quality job.
Even a full workflow rerun retains the first published bundle identity. Conflicts,
changed tags or another workflow run fail without replacing assets. Release CI
artifacts include their attempt in the name and are retained for 14 days. Recovery
after expiry requires manual handling of the original bundle. No assets are deleted
or overwritten automatically.

GitHub Actions builds both installation artifacts in one workflow. The bundle
builder stamps the same version, build number and full Git commit into each
artifact's `build-info.json`. The build number is
`github.run_number.github.run_attempt`, for example `142.2`. It is scoped to the
Checks workflow. A retry has a distinct attempt. The commit identifies source,
while the bundle manifest's SHA256 hashes identify the exact delivered artifacts.
The version/build identity alone is not a binary reproducibility guarantee.

When main reuses an identical fully tested PR candidate, its Actions summary links
the original candidate run, installation artifact and build. That main run creates
no replacement bundle and does not restamp the original revision or build number.
Use the original candidate bundle and verify its manifest and checksums. A missing
or expired candidate artifact causes a full main run instead. See
[review and CI stages](CI-STAGES.md#reuse-an-identical-tested-merge-on-main).

Focused product candidates retain their original matched bundle and separate
selected-suite reports. An identical verified main merge may reuse both only
after the first parent's policy independently confirms the exact required
profile. A focused marker proves selected coverage and never becomes a full
validation marker or deployment authorization. Existing persistent-installation
and release gates remain unchanged. No replacement artifact is built or restamped.

Strict test-only candidates publish test reports without an installation bundle
or deployment build identity. An identical verified main merge may reuse that
profile-bound report evidence after independent first-parent policy verification.
It does not create or replace a deployable artifact. Product updates still need
their own successfully tested matched installation bundle.

Identity is read once when each Python process loads its package. It never reads
the current Git checkout, runtime environment or a changed identity file to claim
an update has loaded. Both artifacts must be installed together. Restart Home
Assistant Core after replacing integration files. Reloading a configuration entry
does not reload imported Python modules or their cached identity. Rebuild/restart
the app as appropriate.
For non-release candidates that publish installation artifacts, CI creates a development bundle with internal version
`0.0.0.dev0` and a real build number. The sidebar displays Development rather than
an official release number. This works before the first GitHub release and for
later untagged builds. Checked-in source manifests are development templates and
are checked for consistency. They do not determine published release versions.
Raw source runs have no CI build number and show Development for both fields.
`package_release` can also package committed local sources without a CI build
number. Such bundles cannot pass numbered-build deployment verification.

## Where to see it

Every app page displays Version and Build at the bottom of the
sidebar. On mobile, open Menu and scroll to its bottom. Only a version/build
mismatch adds an error and the integration's own version and build as visible text.
Matching identities, absent contact and failed status reads show no integration row.
The check refreshes every 15 seconds. Touch users do not need a tooltip to identify
which component differs. The backend expires integration contact after 30 seconds.
This is recent identity evidence, not proof that a process will remain running.
Home Assistant device information also exposes the loaded release version.

To build a tagged bundle explicitly, use:

```console
python -m script.release.package_release .local/bundle --release-tag v0.3.0 --build-number 142.2
```

Release CI supplies both arguments. Other CI runs supply the build number.
Do not invent a CI build number for manual
installations. A published release uses the same version for app and integration.

The existing public setup-session metadata now includes these nonsecret version
details. Credentials are never included. The full deployment-verification endpoint
`GET /v1/version` requires the normal app token or trusted Supervisor ingress.

## Runtime and deployment checks

The integration identifies its loaded version and build in every API
request. The app rejects mismatches before executing the request. The integration
also rejects missing or different response identity. The pinned TLS peer gateway
forwards these identity headers in both directions. A mismatch uses the existing
action-required notification. It accepts no new commands from the mismatched app.
Previously accepted commands retain only their existing validity, following the
existing release-mismatch policy. This change adds no compatibility matrix.

For a user-managed numbered installation with an existing secure loopback connection, run the checker against the expected bundle manifest:

```console
python -m script.release.verify_deployment http://localhost:18099 path/to/release-manifest.json
```

Supply the existing token through `OEP_API_TOKEN`. The checker requires recent
integration contact and exact equality with the expected version and numbered CI
build. Matching each other is insufficient. Both components must match the supplied
manifest, including when intentionally deploying an older bundle. Unnumbered
source builds cannot pass.
The commit stays internal in the package and manifest for maintainers. It is not
displayed or used for runtime compatibility. A failed check exits nonzero.
The checker permits HTTP only on loopback. Use verified HTTPS ingress or an
existing secure loopback tunnel for a remote/native installation. It rejects
redirects and oversized responses. This does not change HA's network settings.
Run this for the numbered product installation being verified. Source-only developer runs stay unnumbered and do not require a transfer bundle. The checker verifies loaded version/build identities,
not device behaviour, solver quality, database integrity or product readiness.
There is no deployment automation in the Checks workflow. Any separately authorized product deployment
automation must invoke this checker after both components are loaded. Personal VM deployment is not part of this repository's developer or merge workflow.

Tests cover artifact identity and hashes, malformed build numbers, both directions
of mismatch detection, expiry and authentication, plus desktop/mobile and ingress
sidebar presentation. Existing real-HA chain tests cover the loaded integration
and the TLS gateway alongside the full app.
