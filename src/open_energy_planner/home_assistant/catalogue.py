"""Validate bounded app-side HA metadata and Energy source membership without HA drivers."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from math import isfinite
from uuid import uuid4

from open_energy_planner.demand.calculation.model import EnergyTerm
from open_energy_planner.home_assistant.catalogue_transfer import (
    CATALOGUE_REQUEST_BYTES,
    CatalogueTransfer,
)
from open_energy_planner.home_assistant.names import display_devices
from open_energy_planner.home_assistant.solar_panels import validate_panel_settings
from open_energy_planner.home_assistant.source_choices import SourceChoices
from open_energy_planner.installation.calculation.annual_estimate import annual_estimate
from open_energy_planner.solar.wire import validate_configuration_suggestions

from ..demand.wire import parse_configuration as parse_demand
from ..installation.model import (
    MAX_BATTERY_NUMERIC_SETTING,
)

MAX_CATALOGUE_REQUEST_BYTES = CATALOGUE_REQUEST_BYTES
DEFAULT_ANNUAL_HOUSEHOLD_KWH = 3500
GENERATED_DEVICE_ID_HEX_LENGTH = 16
MAX_ENTITY_ID_LENGTH = 127
MAX_CATALOGUE_FIELD_LENGTH = 160
CATALOGUE_MAX_AGE_MINUTES = 15
CATALOGUE_REFRESH_REQUEST_SECONDS = 60
COUNTRY_BIDDING_AREA_HINTS = {
    "NL": "NL",
    "BE": "BE",
    "FR": "FR",
    "DE": "DE-LU",
    "LU": "DE-LU",
    "AT": "AT",
    "PL": "PL",
    "FI": "FI",
    "EE": "EE",
    "LV": "LV",
    "LT": "LT",
}
COUNTRY_BIDDING_AREA_OPTIONS = {
    **{country: (area,) for country, area in COUNTRY_BIDDING_AREA_HINTS.items()},
    "DK": ("DK1", "DK2"),
    "NO": ("NO1", "NO2", "NO3", "NO4", "NO5"),
    "SE": ("SE1", "SE2", "SE3", "SE4"),
    "IT": ("IT-NORD", "IT-CNOR", "IT-CSUD", "IT-SUD", "IT-CALA", "IT-SICI", "IT-SARD"),
}
BATTERY_ENERGY_FIELDS = ("soc_entity", "power_entity", "capacity_kwh", "charge_energy_entity")


class HACatalogue:
    """Keep bounded metadata and compact setup statistics in memory, separate from forecast training evidence."""

    def __init__(self):
        """Start explicitly without HA discovery rather than inventing entities."""
        self.value = None
        self.updated = None
        self.confirmed = None
        self.revision = None
        self.requested = None
        self._refresh_started = None
        self.refresh_failed = False
        self.transfer = CatalogueTransfer()
        self.choices = SourceChoices()

    def receive_page(self, body, now):
        """Keep bounded discovery fragments private until their complete snapshot is assembled."""
        return self.transfer.receive(body, now)

    def receive(self, body, now):
        """Validate typed sensor metadata, Energy readiness and compact sum endpoints, excluding arbitrary HA attributes."""
        if not isinstance(body, dict) or set(body) - {"country"} != {
            "entities",
            "suggestions",
            "timezone",
            "history",
        }:
            raise ValueError("Invalid HA catalogue")
        country = body.get("country")
        if country is not None and (
            not isinstance(country, str)
            or len(country) != 2
            or not country.isascii()
            or not country.isalpha()
        ):
            raise ValueError("Invalid Home Assistant country")
        if not isinstance(body["entities"], list):
            raise TypeError("Expected setup entities")
        for entity in body["entities"]:
            if not isinstance(entity, dict) or set(entity) - {
                "device_name",
                "energy_name",
                "integration_id",
                "integration_name",
            } != {
                "entity_id",
                "name",
                "unit",
                "device_id",
                "device_class",
                "state_class",
            }:
                raise ValueError("Invalid setup entity")
            if any(
                not isinstance(value_item, str) or len(value_item) > MAX_CATALOGUE_FIELD_LENGTH
                for value_item in entity.values()
            ):
                raise ValueError("Invalid setup entity metadata")
            if ("integration_id" in entity) != ("integration_name" in entity) or (
                "integration_id" in entity and not entity["integration_id"].strip()
            ):
                raise ValueError("Invalid setup entity metadata")
            if not entity["entity_id"].startswith(("sensor.", "binary_sensor.", "water_heater.")):
                raise ValueError(
                    "Only sensor, binary sensor and water heater metadata is supported"
                )
        hints = body["suggestions"]
        if not isinstance(hints, dict) or set(hints) - {
            "settlement",
            "grid_power_entities",
            "consumers",
            "solar_configurations",
        } != {
            "terms",
            "soc",
            "grid_power",
            "solar",
            "solar_sources",
            "energy_status",
            "batteries",
            "grid_export",
        }:
            raise ValueError("Invalid Energy suggestions")
        consumers = hints.get("consumers", [])
        if not isinstance(consumers, list):
            raise TypeError("Invalid Energy consumers")
        identities = set()
        for consumer in consumers:
            if not isinstance(consumer, dict) or set(consumer) not in (
                {"entity_id", "upstream"},
                {"entity_id", "upstream", "power_entity"},
            ):
                raise ValueError("Invalid Energy consumer")
            EnergyTerm(consumer["entity_id"], 1)
            if consumer.get("power_entity") is not None:
                EnergyTerm(consumer["power_entity"], 1)
            if consumer["entity_id"] in identities:
                raise ValueError("Duplicate Energy consumer")
            identities.add(consumer["entity_id"])
            if consumer["upstream"] is not None:
                EnergyTerm(consumer["upstream"], 1)
        if hints["energy_status"] == "ready" and "grid_power_entities" not in hints:
            raise ValueError("Ready Energy configuration requires its grid power channels")
        if "grid_power_entities" in hints:
            powers = hints["grid_power_entities"]
            if (
                not isinstance(powers, list)
                or len(set(powers)) != len(powers)
                or any(
                    not isinstance(entity, str)
                    or not entity.startswith("sensor.")
                    or len(entity) > MAX_ENTITY_ID_LENGTH
                    for entity in powers
                )
            ):
                raise ValueError("Invalid grid power suggestions")
        if "settlement" in hints:
            roles = hints["settlement"]
            terms = {
                energy_term["entity_id"]: energy_term["sign"] for energy_term in hints["terms"]
            }
            if not isinstance(roles, dict) or set(roles) != {"imports", "exports"}:
                raise ValueError("Invalid grid settlement roles")
            for key, sign in (("imports", 1), ("exports", -1)):
                if not isinstance(roles[key], list) or any(
                    terms.get(entity_item) != sign for entity_item in roles[key]
                ):
                    raise ValueError("Invalid grid settlement bindings")
        # Reuse the domain parser for proposed AC energy channels. Never trust an arbitrary expression.
        parse_demand(
            {
                "timezone": body["timezone"],
                "annual_kwh": DEFAULT_ANNUAL_HOUSEHOLD_KWH,
                "terms": hints["terms"],
                "exclusions": [],
            }
        )
        if (
            not isinstance(hints["soc"], list)
            or any(
                not isinstance(value_item, str) or len(value_item) > MAX_CATALOGUE_FIELD_LENGTH
                for value_item in hints["soc"]
            )
            or not (hints["grid_power"] is None or isinstance(hints["grid_power"], str))
            or type(hints["solar"]) is not bool
            or not isinstance(hints["solar_sources"], list)
            or any(
                not isinstance(value_item, str) or len(value_item) > MAX_CATALOGUE_FIELD_LENGTH
                for value_item in hints["solar_sources"]
            )
        ):
            raise ValueError("Invalid setup suggestions")
        if hints["energy_status"] not in ("missing", "incomplete", "ready"):
            raise ValueError("Invalid Energy status")
        if (hints["energy_status"] == "ready") != bool(hints["terms"]):
            raise ValueError("Energy readiness requires a complete balance")
        if not isinstance(hints["batteries"], list):
            raise TypeError("Invalid battery suggestions")
        for battery in hints["batteries"]:
            if (
                not isinstance(battery, dict)
                or set(battery)
                != {
                    "energy_entity",
                    "soc_entity",
                    "power_entity",
                    "charge_energy_entity",
                    "capacity_kwh",
                }
                or any(
                    value_item is not None
                    and (not isinstance(value_item, str) or len(value_item) > MAX_ENTITY_ID_LENGTH)
                    for key, value_item in battery.items()
                    if key != "capacity_kwh"
                )
            ):
                raise ValueError("Invalid battery suggestion")
            capacity = battery.get("capacity_kwh")
            if capacity is not None and (
                type(capacity) not in (int, float)
                or not isfinite(capacity)
                or not 0 < capacity <= MAX_BATTERY_NUMERIC_SETTING
            ):
                raise ValueError("Invalid Energy battery capacity")
        if hints["grid_export"] is not None and (
            not isinstance(hints["grid_export"], str)
            or len(hints["grid_export"]) > MAX_ENTITY_ID_LENGTH
        ):
            raise ValueError("Invalid grid export suggestion")
        if hints["energy_status"] == "ready":
            meters = [
                *hints["solar_sources"],
                *[battery_item["energy_entity"] for battery_item in hints["batteries"]],
            ]
            positive = {
                energy_term["entity_id"]
                for energy_term in hints["terms"]
                if energy_term["sign"] == 1
            }
            negative = {term["entity_id"] for term in hints["terms"] if term["sign"] == -1}
            if any(
                battery.get("charge_energy_entity") is not None
                and battery["charge_energy_entity"] not in negative
                for battery in hints["batteries"]
            ):
                raise ValueError("Battery charging must be present in the household energy balance")
            if len(set(meters)) != len(meters) or any(
                meter_entity not in positive for meter_entity in meters
            ):
                raise ValueError(
                    "Energy devices must identify distinct supply meters in the household balance"
                )
        annual_estimate(hints["terms"], body["history"])
        validate_configuration_suggestions(
            hints.get("solar_configurations", []), hints["solar_sources"]
        )
        self.value, self.updated = body, now
        self.confirmed = now
        self.revision = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        self.requested = None
        self._refresh_started = None
        self.refresh_failed = False

    def confirm(self, revision, now):
        """Confirm an unchanged published revision without relabelling its original publication time.

        Only the authenticated HA peer calls this while its event watch is
        healthy and unchanged. Unknown revisions or pending partial
        transfers cannot extend freshness. This performs no reconciliation.
        """
        if (
            self.value is None
            or not isinstance(revision, str)
            or revision != self.revision
            or self.transfer.pending is not None
        ):
            raise ValueError("Catalogue confirmation does not match published sources")
        self.confirmed = now

    def _expire_refresh(self, now):
        """Permit a new explicit retry if an offline peer or lost failure reply leaves a request pending."""
        if (
            self.requested is not None
            and self._refresh_started is not None
            and now - self._refresh_started >= timedelta(seconds=CATALOGUE_REFRESH_REQUEST_SECONDS)
        ):
            self.requested = None
            self._refresh_started = None
            self.refresh_failed = True

    def request_refresh(self, now=None):
        """Coalesce pending requests and permit a fresh explicit retry after a reported failure."""
        now = now or datetime.now(UTC)
        self._expire_refresh(now)
        if self.requested is None:
            self.requested = str(uuid4())
            self._refresh_started = now
            self.refresh_failed = False
        return self.requested

    def fail_refresh(self, identifier):
        """Mark only the still-current explicit request unconfirmed, preserving all usable data."""
        if (
            not isinstance(identifier, str)
            or self.requested is None
            or identifier != self.requested
        ):
            raise ValueError("Source refresh request no longer matches")
        self.requested = None
        self._refresh_started = None
        self.refresh_failed = True

    def refresh_status(self, now=None):
        """Expose compact request progress and publication provenance without returning the catalogue."""
        self._expire_refresh(now or datetime.now(UTC))
        return {
            "id": self.requested,
            "pending": self.requested is not None,
            "failed": self.refresh_failed,
            "revision": self.revision,
            "published_at": self.updated.isoformat() if self.updated else None,
            "confirmed_at": self.confirmed.isoformat() if self.confirmed else None,
        }

    def current(self, now):
        """Expose missing/stale discovery explicitly so setup cannot silently use an unknown HA configuration."""
        self._expire_refresh(now)
        return {
            "data": self.value,
            "stale": self.confirmed is None
            or now - self.confirmed > timedelta(minutes=CATALOGUE_MAX_AGE_MINUTES),
            "refresh_pending": self.requested is not None,
        }

    def validate_panel_settings(self, candidate, current, now):
        """Keep write-time panel ownership consistent with the same discovery used for reconciliation."""
        validate_panel_settings(candidate, current, self.current(now))

    def validate_membership(self, kind, configuration):
        """Reject independent device membership once HA has supplied its authoritative Energy sources."""
        if self.value is None:
            return
        hints = self.value["suggestions"]
        if hints["energy_status"] != "ready":
            raise ValueError("Complete the Home Assistant Energy dashboard first")
        if kind == "solar":
            actual = [source_item.entity_id for source_item in configuration.sources]
            expected = hints["solar_sources"]
        elif kind == "demand":
            if configuration.timezone != self.value["timezone"]:
                raise ValueError("Timezone is managed in the Home Assistant installation settings")
            actual = [
                (energy_term.entity_id, energy_term.sign) for energy_term in configuration.terms
            ]
            expected = [
                (energy_term["entity_id"], energy_term["sign"]) for energy_term in hints["terms"]
            ]
        else:
            actual = [battery_item.energy_entity for battery_item in configuration.batteries]
            expected = [battery_item["energy_entity"] for battery_item in hints["batteries"]]
            if "grid_power_entities" in hints:
                powers = hints["grid_power_entities"]
                if (
                    not powers
                    or [
                        configuration.grid_power_entity,
                        *configuration.additional_grid_power_entities,
                    ]
                    != powers
                    or configuration.grid_power_inverted
                ):
                    raise ValueError(
                        "Configure grid power in the Home Assistant Energy dashboard. The normalized sensors are used without additional inversion"
                    )
                if configuration.grid_export_entity != hints["grid_export"]:
                    raise ValueError(
                        "Grid export meters are managed in the Home Assistant Energy dashboard"
                    )
                if (
                    list(configuration.additional_grid_export_entities)
                    != hints.get("settlement", {}).get("exports", [])[1:]
                ):
                    raise ValueError(
                        "Grid export meters are managed in the Home Assistant Energy dashboard"
                    )
            for battery in configuration.batteries:
                hint = next(
                    (
                        item
                        for item in hints["batteries"]
                        if item["energy_entity"] == battery.energy_entity
                    ),
                    None,
                )
                if hint is not None and "capacity_kwh" in hint:
                    for key in BATTERY_ENERGY_FIELDS:
                        if hint.get(key) is None:
                            raise ValueError(
                                f"Configure battery {key} in the Home Assistant Energy dashboard"
                            )
                        if getattr(battery, key) != hint[key]:
                            raise ValueError(
                                "Battery meters, SoC, power and capacity are managed in the Home Assistant Energy dashboard"
                            )
                    if battery.power_inverted:
                        raise ValueError(
                            "Battery power direction is managed in the Home Assistant Energy dashboard"
                        )
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise ValueError("Device membership is managed in the Home Assistant Energy dashboard")

    def configuration_issue(self, installation, solar, demand, *, required=False):
        """Block planning against a known different Energy installation without deleting historical settings."""
        if self.value is None:
            return "waiting_for_energy_configuration" if required else None
        if installation is None and not required:
            return None
        try:
            if installation is None or solar is None or demand is None:
                raise ValueError("Setup required")
            self.validate_membership("installation", installation)
            self.validate_membership("solar", solar)
            if [asdict(energy_term) for energy_term in demand.terms] != self.value["suggestions"][
                "terms"
            ]:
                raise ValueError("Household balance changed")
        except ValueError:
            return "energy_configuration_changed"
        return None

    def devices(self, configured):
        """Project Energy membership immediately. Missing ratings remain explicit setup-required devices."""
        from uuid import NAMESPACE_URL, uuid5

        if self.value is None:
            return configured
        hints = self.value["suggestions"]
        if hints["energy_status"] != "ready":
            return display_devices(configured, self.value)
        result = []
        for kind, entities in (
            ("solar", hints["solar_sources"]),
            ("battery", [battery_item["energy_entity"] for battery_item in hints["batteries"]]),
        ):
            for entity in entities:
                saved = next(
                    (
                        device
                        for device in configured or []
                        if device["kind"] == kind and device.get("energy_entity") == entity
                    ),
                    None,
                )
                result.append(
                    saved
                    or {
                        "id": kind
                        + "_"
                        + uuid5(NAMESPACE_URL, entity).hex[:GENERATED_DEVICE_ID_HEX_LENGTH],
                        "kind": kind,
                        "name": entity,
                        "energy_entity": entity,
                        "configured": False,
                        "can_curtail": False,
                    }
                )
        return display_devices(result, self.value)
