"""Serve explicit bounded source chooser requests without changing active discovery or forecasts."""

from aiohttp import ClientError

from .catalogue import source_choices
from .catalogue_transfer import prepare_catalogue
from .client import AuthenticationError, ReleaseMismatchError

SOURCE_CHOICE_SCOPES = ("ev", "thermal")


async def refresh_choices(coordinator, requests):
    """Attempt each finite request once and report failure without retrying all sources on every poll."""
    if not isinstance(requests, list) or len(requests) > len(SOURCE_CHOICE_SCOPES):
        raise ValueError("Invalid source choice requests")
    if any(
        not isinstance(request, dict)
        or set(request) != {"id", "scope"}
        or request["scope"] not in SOURCE_CHOICE_SCOPES
        or not isinstance(request["id"], str)
        or len(request["id"]) != 32
        for request in requests
    ):
        raise ValueError("Invalid source choice requests")
    identifiers = {request["id"] for request in requests}
    if len(identifiers) != len(requests) or len({request["scope"] for request in requests}) != len(
        requests
    ):
        raise ValueError("Duplicate source choice requests")
    coordinator.choice_requests.intersection_update(identifiers)
    for request in requests:
        if coordinator.closed:
            return
        if request["id"] in coordinator.choice_requests:
            continue
        coordinator.choice_requests.add(request["id"])
        try:
            captured = await source_choices(coordinator.hass, request["scope"])
            if coordinator.closed:
                return
            prepared = await coordinator.hass.async_add_executor_job(prepare_catalogue, captured)
            for page in prepared.pages:
                if coordinator.closed:
                    return
                reply = await coordinator.client.request(
                    "POST", "/v1/ha/source-choices", {**request, "page": page}
                )
            if reply.get("accepted") is not True:
                raise ValueError("Source choice result was not accepted")
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
            if coordinator.closed:
                return
            try:
                await coordinator.client.request(
                    "POST", "/v1/ha/source-choices", {**request, "failed": True}
                )
            except (AuthenticationError, ReleaseMismatchError):
                raise
            except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
                # The app expires the request. A fresh explicit request can retry safely.
                pass
