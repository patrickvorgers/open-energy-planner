"""Serve app pages and assets using the existing ingress policy and browser security headers."""

from hashlib import sha256
from html import escape
from mimetypes import guess_type
from pathlib import Path

from aiohttp import web

from ..build_info import BUILD_INFO

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = WEB / "pages"
PUBLIC = WEB / "public"
PARTIALS = WEB / "partials"
PUBLIC_RESOURCES = web.AppKey("public-page-resources", set)
PAGE_ALIASES = {"/": "/workspace", "/setup/": "/setup", "/consumers": "/quality"}
STATIC_ASSET_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def is_public(request):
    """Expose only GET/HEAD routes actually registered as public pages or static assets."""
    return request.method in {
        "GET",
        "HEAD",
    } and request.match_info.route.resource in request.app.get(PUBLIC_RESOURCES, set())


def register(app, installation, demand, solar):
    """Register page rendering without owning HA catalogue exchange or configuration writes."""

    # Freeze bytes with their address so an in-place development edit cannot poison a cached version.
    public_files = {
        path.relative_to(PUBLIC).as_posix(): path.read_bytes()
        for path in sorted(PUBLIC.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.resolve().is_relative_to(PUBLIC.resolve())
    }
    asset_hash = sha256()
    for name, content in public_files.items():
        asset_hash.update(name.encode() + b"\0" + sha256(content).digest())
    asset_prefix = "/setup/static/" + asset_hash.hexdigest() + "/"

    async def versioned_asset(request):
        """Serve only frozen public bytes under their exact content-addressed namespace."""
        content = public_files.get(request.match_info["filename"])
        if content is None:
            raise web.HTTPNotFound()
        return web.Response(
            body=content,
            content_type=guess_type(request.match_info["filename"])[0]
            or "application/octet-stream",
        )

    pages = {
        "/" + path.relative_to(PAGES).with_suffix("").as_posix(): path
        for path in sorted(PAGES.rglob("*.html"))
        if not path.is_symlink() and path.resolve().is_relative_to(PAGES.resolve())
    }
    routes = {**pages, **{alias: pages[target] for alias, target in PAGE_ALIASES.items()}}
    app[PUBLIC_RESOURCES] = set()

    async def asset(request):
        """Serve fixed assets under a restrictive same-origin browser policy.

        Every reachable HTML shell in the pages directory contains OEP_NAVIGATION,
        replaced by the shared menu. Mark setup, contract, appliances, EV
        charging, thermal storage and quality active here. Overview marks
        Planning/Savings from its hash so switching views preserves chart state.

        Root and /workspace serve the persistent shell. Its page query selects
        a known content route. Invalid choices fall back to /overview after
        installation, demand and solar are configured, otherwise to /setup.
        The selected page loads with embedded=1. Non-shell HTML with that flag
        hides duplicate navigation and retains the flag on content-page links.
        Trusted ingress hides connection forms in the initial HTML without
        changing protected API authentication.
        """
        page = routes[request.path]
        filename = page.relative_to(PAGES).as_posix()
        response = web.FileResponse(page)
        if filename.endswith(".html"):
            from ..ingress import prefix

            base = prefix(request) or ""
            html = page.read_text(encoding="utf-8")
            page_routes = {
                path for path, page_path in routes.items() if page_path.name != "workspace.html"
            }
            if filename == "workspace.html":
                default_page = (
                    "/overview"
                    if installation.current is not None
                    and demand.configuration is not None
                    and solar.configuration is not None
                    else "/setup"
                )
                selected_page = request.query.get("page", default_page)
                if selected_page.split("#", 1)[0] not in page_routes:
                    selected_page = default_page
                html = html.replace(
                    "__OEP_START_PAGE__",
                    escape(
                        base
                        + selected_page.split("#", 1)[0]
                        + "?embedded=1"
                        + ("#" + selected_page.split("#", 1)[1] if "#" in selected_page else ""),
                        quote=True,
                    ),
                )
            elif request.query.get("embedded") == "1":
                html = html.replace('<body class="', '<body class="embedded-workspace ')
            # Trusted ingress is known before first paint, even while session discovery is pending.
            if base:
                for form_id in ("connect", "contract-connect", "overview-connect"):
                    html = html.replace(f'<form id="{form_id}"', f'<form id="{form_id}" hidden')
            navigation = (PARTIALS / "navigation.html").read_text(encoding="utf-8")
            navigation = navigation.replace(
                "__OEP_VERSION__",
                escape(BUILD_INFO["version"] if BUILD_INFO["release_tag"] else "Development"),
            )
            navigation = navigation.replace(
                "__OEP_BUILD__", escape(BUILD_INFO["build"] or "Development")
            )
            active_route = (
                "/appliance-settings"
                if filename.startswith("appliance-settings/")
                else "/" + filename.removesuffix(".html")
            )
            if filename in {"workspace.html", "overview.html"}:
                active_route = None
            if active_route:
                navigation = navigation.replace(
                    f'href="{active_route}"', f'href="{active_route}" aria-current="page"'
                )
            html = html.replace("<!-- OEP_NAVIGATION -->", navigation)
            if filename == "workspace.html":
                html = html.replace('class="sidebar"', 'class="sidebar" inert aria-busy="true"', 1)
            settings_navigation = (PARTIALS / "appliance-settings-navigation.html").read_text(
                encoding="utf-8"
            )
            settings_navigation = settings_navigation.replace(
                f'href="{request.path}"', f'href="{request.path}" aria-current="page"'
            )
            html = html.replace("<!-- OEP_APPLIANCE_SETTINGS -->", settings_navigation)
            html = (
                html.replace('href="/setup/', 'href="' + base + asset_prefix)
                .replace('src="/setup/', 'src="' + base + asset_prefix)
                .replace("__OEP_BASE__", base + "/")
                .replace('href="/overview"', 'href="' + base + '/overview"')
                .replace('href="/overview#', 'href="' + base + "/overview#")
                .replace('href="/setup"', 'href="' + base + '/setup"')
                .replace('href="/contract"', 'href="' + base + '/contract"')
                .replace('href="/data-storage"', 'href="' + base + '/data-storage"')
                .replace('href="/consumers"', 'href="' + base + '/consumers"')
                .replace('href="/appliances"', 'href="' + base + '/appliances"')
                .replace('href="/appliance-settings"', 'href="' + base + '/appliance-settings"')
                .replace('href="/appliance-settings/', 'href="' + base + "/appliance-settings/")
                .replace('href="/ev"', 'href="' + base + '/ev"')
                .replace('href="/thermal"', 'href="' + base + '/thermal"')
                .replace('href="/quality"', 'href="' + base + '/quality"')
                .replace('href="/planning-performance"', 'href="' + base + '/planning-performance"')
                .replace('href="/activity"', 'href="' + base + '/activity"')
            )
            if request.query.get("embedded") == "1" and filename != "workspace.html":
                for route in page_routes:
                    html = html.replace(f'href="{base}{route}"', f'href="{base}{route}?embedded=1"')
                    html = html.replace(f'href="{base}{route}#', f'href="{base}{route}?embedded=1#')
            response = web.Response(text=html, content_type="text/html")
        response.headers.update(SECURITY_HEADERS)
        return response

    for path in routes:
        route = app.router.add_get(path, asset)
        app[PUBLIC_RESOURCES].add(route.resource)
    cached_resource = app.router.add_get(asset_prefix + "{filename:.*}", versioned_asset).resource
    app[PUBLIC_RESOURCES].add(cached_resource)
    resource = app.router.add_static("/setup/", PUBLIC, show_index=False, follow_symlinks=False)
    app[PUBLIC_RESOURCES].add(resource)

    async def prepare_public_response(request, response):
        """Apply the same browser policy to streamed static files, HEAD and page responses."""
        if is_public(request):
            response.headers.update(SECURITY_HEADERS)
            if request.match_info.route.resource is cached_resource and response.status == 200:
                response.headers["Cache-Control"] = (
                    f"public, max-age={STATIC_ASSET_MAX_AGE_SECONDS}, immutable"
                )

    app.on_response_prepare.append(prepare_public_response)
