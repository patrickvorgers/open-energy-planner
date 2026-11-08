"""Own short-lived, complete chooser results separately from active Energy reconciliation."""

from datetime import timedelta
from uuid import uuid4

from open_energy_planner.home_assistant.catalogue_transfer import CatalogueTransfer

CHOICE_SCOPES = ("ev", "thermal")
CHOICE_REQUEST_SECONDS = 60
CHOICE_RESULT_MINUTES = 5


def configured_sources(configuration, ev, thermal):
    """Project explicit persisted bindings, including appliances outside the Energy dashboard."""
    identities = (
        {binding.entity_id for binding in configuration.bindings} if configuration else set()
    )
    for rows, fields in (
        (ev.get("vehicles", ()), ("soc_entity",)),
        (ev.get("points", ()), ("energy_entity", "power_entity", "connected_entity")),
        (thermal.get("stores", ()), ("energy_entity", "temperature_entity", "power_entity")),
    ):
        identities.update(row[field] for row in rows for field in fields if row.get(field))
    return sorted(identities)


class SourceChoices:
    """Keep at most one bounded request and expiring accepted result per supported editor scope."""

    def __init__(self):
        """Start without candidate metadata or pending HA reads."""
        self.requests = {}
        self.results = {}
        self.failed = set()

    def _scope(self, scope):
        """Reject arbitrary query scopes rather than forwarding user-controlled HA queries."""
        if scope not in CHOICE_SCOPES:
            raise ValueError("Unsupported source choice scope")

    def _expire(self, now):
        """Bound waiting and candidate retention without scheduling any periodic metadata reads."""
        for scope, request in tuple(self.requests.items()):
            if now - request["started"] >= timedelta(seconds=CHOICE_REQUEST_SECONDS):
                self.requests.pop(scope)
                self.failed.add(scope)
        for scope, (captured, _body) in tuple(self.results.items()):
            if now - captured >= timedelta(minutes=CHOICE_RESULT_MINUTES):
                self.results.pop(scope)

    def request(self, scope, now):
        """Coalesce concurrent requests while allowing an explicit retry after timeout or failure."""
        self._scope(scope)
        self._expire(now)
        if scope not in self.requests:
            self.requests[scope] = {
                "id": uuid4().hex,
                "started": now,
                "transfer": CatalogueTransfer(),
            }
            self.failed.discard(scope)
        return self.requests[scope]["id"]

    def pending(self, now):
        """Expose only current finite scopes and nonces to the authenticated integration."""
        self._expire(now)
        return [{"scope": scope, "id": request["id"]} for scope, request in self.requests.items()]

    def receive(self, body, now):
        """Accept chooser pages without changing active source membership or freshness.

        Returns:
            True when a complete validated result replaces the scope's choices;
            False for incomplete pages or a reported failure. Completion and
            reported failure remove the request; incomplete pages retain it.

        Raises:
            ValueError: The body, scope, nonce, transfer or catalogue is invalid.
                Expired requests are removed before validation. Other invalid
                responses retain the request until expiry, failure or acceptance;
                invalid transfers discard their partial assembly. Previously
                accepted choices remain available until their own expiry.
        """
        # catalogue.py imports SourceChoices, so defer the validator import.
        from open_energy_planner.home_assistant.catalogue import HACatalogue

        self._expire(now)
        if not isinstance(body, dict) or set(body) not in (
            {"scope", "id", "page"},
            {"scope", "id", "failed"},
        ):
            raise ValueError("Invalid source choice response")
        scope = body["scope"]
        self._scope(scope)
        request = self.requests.get(scope)
        if request is None or body["id"] != request["id"]:
            raise ValueError("Source choice request no longer matches")
        if "failed" in body:
            if body["failed"] is not True:
                raise ValueError("Invalid source choice outcome")
            self.requests.pop(scope)
            self.failed.add(scope)
            return False
        complete = request["transfer"].receive(body["page"], now)
        if complete is None:
            return False
        validator = HACatalogue()
        validator.receive(complete, now)
        self.results[scope] = (now, validator.value)
        self.requests.pop(scope)
        self.failed.discard(scope)
        return True

    def catalogue(self, scope, active, now):
        """Merge fresh candidates for selection validation without changing authoritative Energy data."""
        self._scope(scope)
        self._expire(now)
        if not active or scope not in self.results:
            return active
        entities = {row["entity_id"]: row for row in active["entities"]}
        for row in self.results[scope][1]["entities"]:
            entities.setdefault(row["entity_id"], row)
        return {**active, "entities": list(entities.values())}

    def status(self, scope, active, now):
        """Return progress and editor-only choices without modifying any saved configuration."""
        self._scope(scope)
        self._expire(now)
        request = self.requests.get(scope)
        return {
            "id": request["id"] if request else None,
            "pending": request is not None,
            "failed": scope in self.failed,
            "catalogue": self.catalogue(scope, active, now) if request is None else None,
        }
