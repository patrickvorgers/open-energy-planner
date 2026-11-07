"""Own event-driven sanitized metadata and bounded recovery, separate from current measurements."""

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.const import (
    EVENT_RECORDER_5MIN_STATISTICS_GENERATED,
    EVENT_RECORDER_HOURLY_STATISTICS_GENERATED,
)
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import State, callback
from homeassistant.core_config import EVENT_CORE_CONFIG_UPDATE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_added_domain,
    async_track_state_change_event,
    async_track_state_removed_domain,
)
from homeassistant.util import dt as dt_util

from . import catalogue
from .const import DOMAIN
from .setup_history import setup_history

SOURCE_DOMAINS = ("sensor", "binary_sensor", "water_heater")
ENERGY_MANAGER_KEY = f"{DOMAIN}.catalogue_energy_manager"
ENERGY_CHANGED = f"{DOMAIN}.catalogue_energy_changed"
METADATA_ATTRIBUTES = ("friendly_name", "unit_of_measurement", "device_class", "state_class")


class CatalogueDiscovery:
    """Cache metadata with entry-scoped subscriptions and copy-on-change snapshots.

    All HA reads and cache changes run on the event loop. Returned bodies remain
    detached from subsequent events and can be serialized in an executor. Only
    acknowledged bodies authorize cheap freshness confirmation or forecast context.
    """

    def __init__(self, hass):
        """Start uninitialised and require a full capture before acknowledging any metadata."""
        self.hass = hass
        self.body = None
        self.accepted = None
        self.generation = 0
        self.captured_generation = None
        self._full = True
        self._energy = False
        self._header = False
        self._history = False
        self._entities = set()
        self._device_entities = {}
        self._entry_entities = {}
        self._forecast_entries = set()
        self._names = {}
        self._configured = set()
        self._active = set()
        self._history_day = None
        self._unsubscribers = []
        self._state_cancel = None
        self._watched = set()
        self._started = False
        self._closed = False

    @property
    def healthy(self):
        """Require live subscriptions, no pending changes and the acknowledged snapshot."""
        return (
            self._started
            and not self._closed
            and self.body is self.accepted
            and self.body is not None
            and not (self._full or self._energy or self._header or self._history or self._entities)
        )

    async def start(self):
        """Subscribe before initial reads and share only one HA-lifetime Energy forwarder."""
        if self._started or self._closed:
            return
        self._started = True
        self._unsubscribers = [
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed),
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed),
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._core_changed),
            self.hass.bus.async_listen(
                EVENT_RECORDER_HOURLY_STATISTICS_GENERATED, self._history_changed
            ),
            self.hass.bus.async_listen(
                EVENT_RECORDER_5MIN_STATISTICS_GENERATED, self._history_changed
            ),
            async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed),
            async_dispatcher_connect(self.hass, ENERGY_CHANGED, self._energy_changed),
            async_track_state_added_domain(self.hass, SOURCE_DOMAINS, self._state_changed),
            async_track_state_removed_domain(self.hass, SOURCE_DOMAINS, self._state_changed),
        ]
        self._watch_states()
        try:
            manager = await async_get_manager(self.hass)
        except BaseException:
            if not self._closed:
                self.close()
                self._closed = False
                self._started = False
            raise
        if self._closed:
            return
        if self.hass.data.get(ENERGY_MANAGER_KEY) is not manager:
            hass = self.hass

            async def energy_changed():
                """Forward preferences without retaining an entry or a closed discovery instance."""
                async_dispatcher_send(hass, ENERGY_CHANGED)

            manager.async_listen_updates(energy_changed)
            self.hass.data[ENERGY_MANAGER_KEY] = manager

    @callback
    def close(self):
        """Detach entry callbacks and reject late changes without leaking listeners across reloads."""
        self._closed = True
        for cancel in self._unsubscribers:
            cancel()
        self._unsubscribers.clear()
        if self._state_cancel:
            self._state_cancel()
            self._state_cancel = None

    def acknowledge(self, body):
        """Remember only the raw capture whose normalized wire result was accepted by the app."""
        self.accepted = body

    def repair(self):
        """Require complete publication after a peer loses or rejects the acknowledged revision."""
        self.accepted = None
        self._full = True
        self._changed()

    def configure(self, identities):
        """Replace app-owned extra bindings without scanning arbitrary strings in its snapshot."""
        if not isinstance(identities, list) or any(
            not isinstance(identity, str)
            or len(identity) > catalogue.MAX_ENTITY_ID_LENGTH
            or identity.partition(".")[0] not in SOURCE_DOMAINS
            for identity in identities
        ):
            raise ValueError("Invalid active catalogue sources")
        configured = set(identities)
        if configured != self._configured:
            self._configured = configured
            self._energy = True
            self._changed()

    @callback
    def _changed(self):
        """Retain a generation marker so events arriving during an awaited read cannot be lost."""
        self.generation += 1

    def _relevant(self, identity):
        """Exclude unsupported domains and our own virtual entities before scheduling metadata work."""
        if not isinstance(identity, str) or identity.partition(".")[0] not in SOURCE_DOMAINS:
            return False
        if identity not in self._active:
            return False
        entry = er.async_get(self.hass).async_get(identity)
        return entry is None or entry.platform != DOMAIN

    @callback
    def _state_changed(self, event):
        """Ignore value-only changes and retain only additions, removals or projected metadata changes."""
        if self._closed:
            return
        identity = event.data.get("entity_id")
        if not isinstance(identity, str):
            self.repair()
            return
        if not self._relevant(identity):
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if (
            old is not None
            and new is not None
            and all(
                old.attributes.get(field) == new.attributes.get(field)
                for field in METADATA_ATTRIBUTES
            )
        ):
            return
        self._entities.add(identity)
        self._changed()

    @callback
    def _registry_changed(self, event):
        """Follow renames and removals using both identities, repairing unknown event shapes fully."""
        if self._closed:
            return
        if event.data.get("action") not in ("create", "update", "remove") or not isinstance(
            event.data.get("entity_id"), str
        ):
            self.repair()
            return
        identities = {event.data.get("entity_id"), event.data.get("old_entity_id")}
        relevant = {identity for identity in identities if self._relevant(identity)}
        if relevant:
            self._entities.update(relevant)
            self._changed()

    @callback
    def _device_changed(self, event):
        """Refresh only entities on the changed device, including entries removed from its registry."""
        if self._closed:
            return
        identity = event.data.get("device_id")
        if not isinstance(identity, str) or event.data.get("action") not in (
            "create",
            "update",
            "remove",
        ):
            self.repair()
            return
        identities = set(self._device_entities.get(identity, ()))
        identities.update(
            entry.entity_id
            for entry in er.async_entries_for_device(er.async_get(self.hass), identity)
            if self._relevant(entry.entity_id)
        )
        if identities:
            self._entities.update(identities)
            self._changed()

    @callback
    def _entry_changed(self, _change, entry):
        """Recheck associated source labels and selected provider geometry, never arbitrary entry data."""
        if self._closed or entry.domain == DOMAIN:
            return
        identities = set(self._entry_entities.get(entry.entry_id, ()))
        identities.update(
            item.entity_id
            for item in er.async_entries_for_config_entry(er.async_get(self.hass), entry.entry_id)
            if self._relevant(item.entity_id)
        )
        if entry.entry_id in self._forecast_entries:
            self._energy = True
        if identities or self._energy:
            self._entities.update(identities)
            self._changed()

    @callback
    def _energy_changed(self):
        """Rebuild Energy dependencies while retaining unchanged entity projections."""
        if not self._closed:
            self._energy = True
            self._history = True
            self._changed()

    @callback
    def _core_changed(self, _event):
        """Refresh country/timezone and water-heater units after authoritative HA configuration changes."""
        if not self._closed:
            self._header = True
            self._entities.update(
                identity for identity in self._active if identity.startswith("water_heater.")
            )
            self._changed()

    @callback
    def _history_changed(self, event):
        """Keep compact yearly endpoints independent from entity discovery and ordinary measurements."""
        if self._closed or self.body is None:
            return
        if (
            event.event_type == EVENT_RECORDER_HOURLY_STATISTICS_GENERATED
            or not self.body["history"]
            or self._history_day != dt_util.utcnow().date()
        ):
            self._history = True
            self._changed()

    def _watch_states(self):
        """Use HA's entity-indexed event routing instead of a global state-change listener."""
        if self._closed:
            return
        identities = self._active.copy()
        if identities == self._watched and self._state_cancel is not None:
            return
        if self._state_cancel:
            self._state_cancel()
        self._watched = identities
        self._state_cancel = async_track_state_change_event(
            self.hass, identities, self._state_changed
        )

    async def capture(self, *, force=False, now=None):
        """Read changed projections without periodic metadata audits.

        Initialisation, explicit refresh and detected repair require a complete
        capture. Changes arriving during asynchronous reads retain a full repair
        request and cannot acknowledge a mixed generation.
        """
        if self._closed:
            return None
        if not self._started:
            await self.start()
        if self._closed:
            return None
        now = now or dt_util.utcnow()
        full = force or self._full or self.body is None
        if not full and not (self._entities or self._energy or self._header or self._history):
            return None
        generation = self.generation
        dirty, energy, history = self._entities, self._energy, self._history
        self._entities = set()
        self._full = self._energy = self._header = self._history = False
        try:
            manager = await async_get_manager(self.hass)
            if self._closed:
                return None
            names = catalogue.energy_display_names(manager.data)
            active = catalogue.energy_entities(manager.data) | self._configured
            self._active = active
            self._watch_states()
            if full:
                body = await catalogue.setup_catalogue(self.hass, selected=active)
            else:
                previous = self.body
                suggestions = (
                    await catalogue.energy_suggestions(self.hass)
                    if energy
                    else previous["suggestions"]
                )
                entities = {item["entity_id"]: item for item in previous["entities"]}
                if energy:
                    dirty.update(active ^ set(entities))
                    dirty.update(
                        identity
                        for identity in names.keys() | self._names.keys()
                        if names.get(identity) != self._names.get(identity)
                    )
                for identity in dirty:
                    if identity not in active:
                        entities.pop(identity, None)
                        continue
                    state = self.hass.states.get(identity)
                    if state is None:
                        state = State(identity, STATE_UNKNOWN)
                    projected = (
                        catalogue.catalogue_entity(self.hass, state, names) if state else None
                    )
                    if projected is None:
                        entities.pop(identity, None)
                    else:
                        entities[identity] = projected
                body = {
                    "entities": catalogue.ordered_entities(
                        self.hass, entities.values(), suggestions
                    )
                    if dirty or energy
                    else previous["entities"],
                    "suggestions": suggestions,
                    "history": await setup_history(self.hass, suggestions["terms"])
                    if history
                    else previous["history"],
                    "timezone": self.hass.config.time_zone,
                    "country": self.hass.config.country,
                }
            if self._closed:
                return None
            if generation != self.generation:
                self._full = True
                return None
            self.body = body
            self.captured_generation = generation
            self._names = names
            self._forecast_entries = {
                identity
                for source in (manager.data or {}).get("energy_sources", ())
                if source.get("type") == "solar"
                and isinstance(source.get("config_entry_solar_forecast"), list)
                for identity in (source.get("config_entry_solar_forecast") or ())
                if isinstance(identity, str)
            }
            self._device_entities = {}
            self._entry_entities = {}
            for item in body["entities"]:
                self._device_entities.setdefault(item["device_id"], set()).add(item["entity_id"])
                if item.get("integration_id"):
                    self._entry_entities.setdefault(item["integration_id"], set()).add(
                        item["entity_id"]
                    )
            self._watch_states()
            if full or history:
                self._history_day = now.date()
            if not force and self.accepted is not None and body == self.accepted:
                self.accepted = body
                return None
            return body
        except BaseException:
            self._full = True
            raise
