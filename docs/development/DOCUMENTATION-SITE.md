# Documentation site

The static guide uses MkDocs and the existing Courtyard identity. Markdown under
`docs/users`, `docs/development` and `design` stays canonical. The build includes
only those maintained guides, existing developer-page assets, documentation assets
and selected root contribution/legal files. Application source and local data
are not published. Code references link to their GitHub source instead.

## Local build and preview

Create a separate environment at the repository root:

```console
python -m venv .local/docs-venv
```

Activate `.local/docs-venv/Scripts/Activate.ps1` on Windows PowerShell, or
`. .local/docs-venv/bin/activate` on Linux/macOS. Install the separate pinned
documentation requirements and build:

```console
python -m pip install -r docs/site/requirements.txt
python -m mkdocs build --strict --config-file docs/site/mkdocs.yml
python -m http.server 18766 --bind 127.0.0.1 --directory .local/documentation
```

Open `http://127.0.0.1:18766/open-energy-planner/`. This serves the production
build beneath the actual future project prefix, including direct article URLs,
local fonts and the search worker. The root forwards to the guide. Ctrl+C stops
the server. The server binds only to loopback and requires no GitHub hosting.
Changes need a rebuild. The build is generated under `.local` and is not committed.

Search uses MkDocs' bundled local index and worker. It sends no search text to an
external service. Without JavaScript, articles and the complete navigation remain
readable. Search itself needs JavaScript and browser worker support.

## Maintain one source

`docs/assets/courtyard.css` owns the shared Courtyard site palette, local Outfit and Space Grotesk
font loading, logo presentation, buttons, fields and focus states. Both the wiki
template and provisioning page load it. Reuse the existing app logo and font files
through the site build. Keep reading/navigation layout in `documentation.css` and
the installation-entry layout in `docs/site/provisioning/style.css`. Do not copy
the shared brand definitions into those page styles or load the app's behavior.

Update the user route when a feature changes setup, visible names, defaults,
supported platforms, everyday behavior, limitations or recovery. Update the
responsible detailed guide when its contracts change. Do not copy a detailed
reference into a second maintained page. Add navigation in `docs/site/mkdocs.yml`
when a new subject needs a visible destination. Search also indexes guides not
listed in the short navigation.

Every functional PR must assess documentation-site impact and either update the
affected guides in the same change or explain why no update is needed. Reviewers
must check this decision. Keep temporary screenshots, Taste review notes and test
receipts outside maintained guides. An explanatory product screenshot may remain
when it helps users understand the current interface.

Run the strict build and documentation link tests after guide changes. Check the
production build on desktop, tablet and phone after navigation or theme changes.
Verify search, keyboard operation and a direct nested URL under the project prefix.
Site-tooling changes retain whatever suites the immutable CI ownership policy
actually selects. Do not weaken the selector to classify new build code as prose.

Run the focused publication-boundary tests with
`python -m unittest discover -s docs/site -p test_site.py -v` in the documentation
environment. MkDocs and Lunr notices accompany the static search assets in
`docs/assets/SITE-LICENSES.txt`. The bundled OEP fonts retain their original
license files and source/hash metadata in the generated `assets/fonts` directory.

## GitHub Pages publishing

The public guide is hosted at `https://patrickvorgers.github.io/open-energy-planner/`.
Its root forwards to `docs/`. The provisioning entry is at `provisioning/`, built
from the static sources in `docs/site/provisioning/`.

The `pages.yaml` workflow is the only Pages publisher. It builds
the wiki and developer entry into one `_site` artifact. Relevant pull requests
run the strict build, publication-boundary tests and focused desktop/tablet/mobile
browser checks without deploying. Main updates and manual main dispatch publish
only when `DEVELOPER_PAGES_ENABLED=true`. The `github-pages` environment allows
only `main`, and the deploy job has the only Pages write and identity permissions.
Never add a second publisher that replaces this artifact with a partial site.

Only the generated `_site` directory is uploaded. Source, local databases,
credentials and build hooks remain outside the site. Before pushing, review new
guides and screenshots for a public audience. Publishing documentation is not
proof of an app release or a successfully provisioned Home Assistant installation.
After deployment, verify the root, a direct article, search, local fonts, mobile
navigation and `/provisioning/` on the public URL.
