"""Immutable solar installation, weather and forecast contracts without library objects."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from functools import cached_property
from math import isfinite

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.planning_policy import planning_grid
from open_energy_planner.prices.model import aware

DEFAULT_PANEL_TILT_DEGREES = 30
MAX_SOLAR_POWER_W = 10000000.0
MAX_SOLAR_ENERGY_KWH = 10000000.0
MAX_PANEL_TILT_DEGREES = 90
MIN_WEATHER_TEMPERATURE_CELSIUS = -90
DEFAULT_INVERTER_EFFICIENCY = 0.96
MIN_INVERTER_EFFICIENCY = 0.5
MAX_DEVICES_PER_TYPE = 16
MAX_WEATHER_TEMPERATURE_CELSIUS = 65
MAX_WIND_SPEED_M_S = 120
MAX_IRRADIANCE_W_M2 = 2000
MIN_POSITIVE_SOLAR_SETTING = 1e-09
MAX_PANEL_AZIMUTH_DEGREES = 360
CAPACITY_TOLERANCE_W = 1e-06
MAX_PANEL_NAME_LENGTH = 200
ORIENTATION_AZIMUTHS = {
    "north": (0,),
    "east": (90,),
    "south": (180,),
    "west": (270,),
    "east-west": (90, 270),
}


def version(value):
    """Hash normalized immutable evidence without storing mutable current pointers in plans."""
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def number(value, lower, upper):
    """Require bounded finite physical values, rejecting booleans and missing quantities."""
    if type(value) not in (int, float) or not lower <= value <= upper or not isfinite(value):
        raise ValueError("Invalid solar quantity")


def identity(value):
    """Require bounded stable installation identities usable in persisted source mappings."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value):
        raise ValueError("Invalid solar identity")


@dataclass(frozen=True)
class SolarPanel:
    """One independently oriented plane behind a production meter, in Wp and degrees."""

    panel_id: str
    capacity_w: float
    azimuth: float
    tilt: float
    name: str = ""

    def __post_init__(self):
        """Keep exact geometry and a stable plane identity without inferring electrical wiring."""
        identity(self.panel_id)
        if not isinstance(self.name, str) or len(self.name) > MAX_PANEL_NAME_LENGTH:
            raise ValueError("Invalid solar panel name")
        number(self.capacity_w, MIN_POSITIVE_SOLAR_SETTING, MAX_SOLAR_POWER_W)
        number(self.azimuth, 0, MAX_PANEL_AZIMUTH_DEGREES)
        number(self.tilt, 0, MAX_PANEL_TILT_DEGREES)
        for field in ("capacity_w", "azimuth", "tilt"):
            object.__setattr__(self, field, float(getattr(self, field)))


@dataclass(frozen=True)
class SolarImport:
    """Accepted read-only HA panel configuration retained in immutable OEP provenance."""

    adapter: str
    entry_id: str
    panels: tuple[SolarPanel, ...]
    inverter_limit_w: float | None = None

    def __post_init__(self):
        """Validate bounded provider-neutral provenance without retaining any credentials."""
        identity(self.adapter)
        identity(self.entry_id)
        if (
            not isinstance(self.panels, tuple)
            or not 1 <= len(self.panels) <= MAX_DEVICES_PER_TYPE
            or any(not isinstance(panel, SolarPanel) for panel in self.panels)
            or len({panel.panel_id for panel in self.panels}) != len(self.panels)
        ):
            raise ValueError("Invalid imported solar panels")
        if self.inverter_limit_w is not None:
            number(self.inverter_limit_w, 1, MAX_SOLAR_POWER_W)
            object.__setattr__(self, "inverter_limit_w", float(self.inverter_limit_w))


class ForecastSource(StrEnum):
    """Stable solar source choices shared by settings, selection and archived decisions."""

    AUTOMATIC = "automatic"
    PVWATTS = "pvwatts"
    HOME_ASSISTANT = "ha"
    ML = "ml"


@dataclass(frozen=True)
class SolarSource:
    """Independently measured PV array/group. Wp is panel DC rating, not inverter AC rating."""

    source_id: str
    entity_id: str
    inverter_id: str
    capacity_w: float
    orientation: str
    tilt: float = DEFAULT_PANEL_TILT_DEGREES
    measurement_boundary: str = "ac"
    can_curtail: bool = False
    panels: tuple[SolarPanel, ...] = ()
    ha_imports: tuple[SolarImport, ...] = ()
    forecast_source: ForecastSource = ForecastSource.AUTOMATIC

    def __post_init__(self):
        """Validate minimum inputs and make default tilt and measurement boundary explicit."""
        identity(self.source_id)
        identity(self.inverter_id)
        if not isinstance(self.entity_id, str) or not re.fullmatch(
            r"sensor\.[a-z0-9_]{1,120}", self.entity_id
        ):
            raise ValueError("Solar production requires a sensor")
        number(self.capacity_w, 1, MAX_SOLAR_POWER_W)
        number(self.tilt, 0, MAX_PANEL_TILT_DEGREES)
        if self.orientation not in ("north", "east", "south", "west", "east-west"):
            raise ValueError("Unsupported approximate orientation")
        if self.measurement_boundary not in ("ac", "dc") or type(self.can_curtail) is not bool:
            raise ValueError("Invalid solar measurement/control boundary")
        object.__setattr__(self, "forecast_source", ForecastSource(self.forecast_source))
        object.__setattr__(self, "capacity_w", float(self.capacity_w))
        object.__setattr__(self, "tilt", float(self.tilt))
        if (
            not isinstance(self.panels, tuple)
            or len(self.panels) > MAX_DEVICES_PER_TYPE
            or any(not isinstance(panel, SolarPanel) for panel in self.panels)
            or len({panel.panel_id for panel in self.panels}) != len(self.panels)
        ):
            raise ValueError("Invalid solar panel geometry")
        if (
            self.panels
            and abs(sum(panel.capacity_w for panel in self.panels) - self.capacity_w)
            > CAPACITY_TOLERANCE_W
        ):
            raise ValueError("Solar panel capacities must equal their metered group capacity")
        if (
            not isinstance(self.ha_imports, tuple)
            or len(self.ha_imports) > MAX_DEVICES_PER_TYPE
            or any(not isinstance(provider, SolarImport) for provider in self.ha_imports)
            or len({(provider.adapter, provider.entry_id) for provider in self.ha_imports})
            != len(self.ha_imports)
        ):
            raise ValueError("Invalid solar import provenance")

    @property
    def geometry(self):
        """Use exact planes when supplied, otherwise preserve the declared coarse manual orientation."""
        if self.panels:
            return self.panels
        azimuths = ORIENTATION_AZIMUTHS[self.orientation]
        return tuple(
            SolarPanel(str(index), self.capacity_w / len(azimuths), azimuth, self.tilt)
            for index, azimuth in enumerate(azimuths)
        )


@dataclass(frozen=True)
class SolarInverter:
    """Shared inverter rating and declared conversion, independent of hardware brands."""

    inverter_id: str
    ac_limit_w: float
    efficiency: float = DEFAULT_INVERTER_EFFICIENCY
    hybrid: bool = False

    def __post_init__(self):
        """Keep shared AC limits hard and reject unsupported efficiency assumptions."""
        identity(self.inverter_id)
        number(self.ac_limit_w, 1, MAX_SOLAR_POWER_W)
        number(self.efficiency, MIN_INVERTER_EFFICIENCY, 1)
        if type(self.hybrid) is not bool:
            raise ValueError("Invalid hybrid flag")
        object.__setattr__(self, "ac_limit_w", float(self.ac_limit_w))
        object.__setattr__(self, "efficiency", float(self.efficiency))


@dataclass(frozen=True)
class SolarConfiguration:
    """App-owned disjoint PV sources and inverter links. An empty tuple explicitly means no PV."""

    sources: tuple[SolarSource, ...]
    inverters: tuple[SolarInverter, ...]

    def __post_init__(self):
        """Require unique meters/IDs, referenced inverter instances and bounded configuration."""
        if not isinstance(self.sources, tuple) or not isinstance(self.inverters, tuple):
            raise TypeError("Expected immutable solar configuration")
        if len(self.sources) > MAX_DEVICES_PER_TYPE or len(self.inverters) > MAX_DEVICES_PER_TYPE:
            raise ValueError("Too many solar instances")
        if any(not isinstance(source, SolarSource) for source in self.sources) or any(
            not isinstance(inverter, SolarInverter) for inverter in self.inverters
        ):
            raise TypeError("Expected typed solar instances")
        ids = {inverter.inverter_id for inverter in self.inverters}
        if len(ids) != len(self.inverters) or ids != {
            source.inverter_id for source in self.sources
        }:
            raise ValueError("Inverter references must match configured sources")
        for values in (
            [source.source_id for source in self.sources],
            [source.entity_id for source in self.sources],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Duplicated solar source or measurement")

    @property
    def version(self):
        """Bind output, learned parameters and history requests to exact source definitions."""
        body = asdict(self)
        for source in body["sources"]:
            for group in (source, *source["ha_imports"]):
                for panel in group["panels"]:
                    if not panel["name"]:
                        panel.pop("name")
            for field in ("panels", "ha_imports"):
                if not source[field]:
                    source.pop(field)
            if source["forecast_source"] == ForecastSource.AUTOMATIC:
                source.pop("forecast_source")
        return version(body)


@dataclass(frozen=True)
class SolarWeatherHour:
    """Mean weather for [start,start+1h), normalized from provider end-labelled radiation."""

    start: datetime
    ghi: float
    dni: float
    dhi: float
    celsius: float
    wind_ms: float
    received_at: datetime

    def __post_init__(self):
        """Reject incomplete/invalid hours and retain when evidence actually became available."""
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "received_at", aware(self.received_at))
        if self.start.minute or self.start.second or self.start.microsecond:
            raise ValueError("Weather requires UTC hours")
        for value in (self.ghi, self.dni, self.dhi):
            number(value, 0, MAX_IRRADIANCE_W_M2)
        number(self.celsius, MIN_WEATHER_TEMPERATURE_CELSIUS, MAX_WEATHER_TEMPERATURE_CELSIUS)
        number(self.wind_ms, 0, MAX_WIND_SPEED_M_S)


@dataclass(frozen=True)
class SolarEnergy:
    """One source's interval energy: available panel DC and standalone-equivalent AC in kWh."""

    source_id: str
    start: datetime
    duration_h: float
    dc_kwh: float
    ac_kwh: float

    def __post_init__(self):
        """Keep finite nonnegative energies and explicit interval boundaries."""
        identity(self.source_id)
        object.__setattr__(self, "start", aware(self.start))
        number(self.duration_h, MIN_POSITIVE_SOLAR_SETTING, 1)
        number(self.dc_kwh, 0, MAX_SOLAR_ENERGY_KWH)
        number(self.ac_kwh, 0, MAX_SOLAR_ENERGY_KWH)


@dataclass(frozen=True)
class SolarForecast:
    """Exact per-source forecast and provenance. Hourly weather projection is not native quarter detail."""

    configuration_version: str
    location: SiteLocation
    issued_at: datetime
    weather_version: str
    energies: tuple[SolarEnergy, ...]
    model: str = "pvwatts-v2"
    quality: str = "provisional"
    calibration_versions: tuple[str, ...] = ()
    valid_until: datetime | None = None

    @cached_property
    def version(self):
        """Identify the archived prediction without recomputing historical plans."""
        return version(asdict(self))


def horizon(now):
    """Build the shared UTC planning grid from the central horizon and resolution policy."""
    return planning_grid(aware(now))
