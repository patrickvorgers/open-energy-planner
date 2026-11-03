"""Follow confirmed HA panel geometry without owning physical inverter settings."""

from dataclasses import dataclass, replace
from enum import StrEnum

from open_energy_planner.solar.calculation.model import SolarImport, SolarSource
from open_energy_planner.solar.wire import parse_import


class PanelState(StrEnum):
    """Separate confirmed manual ownership from available or temporarily missing HA settings."""

    MANUAL = "manual"
    FOLLOWING = "following"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PanelSettings:
    """Carry validated provider geometry or the last accepted provider snapshot."""

    state: PanelState
    imports: tuple[SolarImport, ...] = ()

    @property
    def managed(self):
        """Keep HA ownership during outages instead of silently enabling manual overrides."""
        return self.state != PanelState.MANUAL

    def document(self):
        """Expose ownership and availability without making provider data another editable draft."""
        return {"state": self.state.value, "managed": self.managed}


def panel_settings(catalogue, entity_id, previous=None):
    """Accept unlinking only from a fresh, complete, explicit HA discovery result.

    An absent group, unreadable provider or stale catalogue never proves removal.
    Persisted imports preserve ownership through restart until HA confirms otherwise.
    """
    retained = previous.ha_imports if previous is not None else ()
    unknown = PanelSettings(PanelState.UNAVAILABLE if retained else PanelState.MANUAL, retained)
    body = catalogue.get("data")
    if not body or catalogue["stale"] or body["suggestions"]["energy_status"] != "ready":
        return unknown
    proposal = next(
        (
            group
            for group in body["suggestions"].get("solar_configurations", ())
            if group["entity_id"] == entity_id
        ),
        None,
    )
    if proposal is None:
        return unknown
    if proposal["issue"] == "solar_configuration_not_available":
        return PanelSettings(PanelState.MANUAL)
    if proposal["issue"] is not None:
        return PanelSettings(PanelState.UNAVAILABLE, retained)
    return PanelSettings(
        PanelState.FOLLOWING, tuple(parse_import(provider) for provider in proposal["providers"])
    )


def follow_panels(source: SolarSource, settings: PanelSettings):
    """Replace complete linked planes, retain outage geometry, or release confirmed manual ownership.

    Converter identity, AC rating, efficiency, hybrid status and curtailment settings
    remain independently configured. Forecast-model inverter limits are provenance only.
    """
    if settings.state == PanelState.FOLLOWING:
        panels = tuple(panel for provider in settings.imports for panel in provider.panels)
        return replace(
            source,
            panels=panels,
            capacity_w=sum(panel.capacity_w for panel in panels),
            ha_imports=settings.imports,
        )
    if settings.state == PanelState.MANUAL:
        return replace(source, ha_imports=())
    return source


def validate_panel_settings(candidate, current, catalogue):
    """Reject HTTP overrides of HA-owned geometry, including while its metadata is unavailable."""
    previous = {source.entity_id: source for source in current.sources} if current else {}
    for source in candidate.sources:
        retained = previous.get(source.entity_id)
        settings = panel_settings(catalogue, source.entity_id, retained)
        if settings.managed:
            if settings.state == PanelState.UNAVAILABLE and retained is None:
                raise ValueError("Wait for complete Home Assistant panel settings")
            expected = follow_panels(retained or source, settings)
            if (source.capacity_w, source.geometry, source.ha_imports) != (
                expected.capacity_w,
                expected.geometry,
                expected.ha_imports,
            ):
                raise ValueError(
                    "Panel settings are managed in Home Assistant. Reload before saving"
                )
        elif source.ha_imports:
            raise ValueError("The solar forecast link changed. Reload before saving")
