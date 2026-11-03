"""Expose bounded public setup metadata without disclosing credentials."""

from aiohttp import web

from open_energy_planner.build_info import BUILD_STATUS, HEADER_FIELDS
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.home_assistant.names import energy_source_names


def register(app, catalogue):
    """Register the public session projection using the app-owned catalogue and identity reader."""

    async def session(request):
        """Return unauthenticated GET setup metadata without returning a token.

        Report trusted ingress and the HA installation timezone, or None before
        discovery. Browser headers cannot establish ingress trust.
        ``build_info`` contains the loaded app identity, last observed integration
        identity, ``integration_last_seen`` timestamp and ``status`` of ``matched``,
        ``mismatch`` or ``unavailable``. Matching requires recent observed contact
        and equal version/build identities, not continuous integration availability.
        App metadata contains only version and build. No credentials or commit
        provenance are included. Persistent blocker flags require authentication.
        """
        from open_energy_planner.ingress import prefix

        build_status = request.app[BUILD_STATUS]()
        build_status = {
            field: build_status[field]
            for field in ("app", "integration", "integration_last_seen", "status")
        }
        build_status["app"] = {field: build_status["app"][field] for field in HEADER_FIELDS}
        return web.json_response(
            {
                "ingress": prefix(request) is not None,
                "timezone": (catalogue.value or {}).get("timezone"),
                "source_names_revision": digest(energy_source_names(catalogue.value)),
                "build_info": build_status,
            },
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get("/setup/session", session)
