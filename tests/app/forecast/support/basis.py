"""Typed current-installation inputs for cached solar and household projection tests."""

from types import SimpleNamespace

from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandForecast,
    EnergyTerm,
    ForecastStep,
)
from open_energy_planner.home_assistant.observations import Observation, ObservationReport
from open_energy_planner.installation.basis import InstallationBasisSource
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.installation.model import Installation
from open_energy_planner.installation.storage import sources
from open_energy_planner.solar.calculation.model import horizon


def forecast_basis(
    now, solar, *, demand=None, load_steps=None, settings: Installation | None = None
):
    """Borrow runtime caches behind supplied physical settings and a matching current HA report.

    The default installation is battery-free at 4 kW. Supplied batteries receive
    independent 50% readings that individual tests can replace.
    Default household energy is independently one kWh per hour. Tests supply
    other rates and bounded coverage through typed ForecastStep records.
    No predictor, provider, persistent configuration or forecast is changed.
    """
    settings = settings or Installation("sensor.grid", 4000, 4000)
    configuration = sources(settings)
    location = SiteLocation(52, 6, "Europe/Amsterdam")
    report = ObservationReport(
        "projection-report",
        configuration.revision,
        now,
        tuple(
            Observation(
                binding.key,
                binding.entity_id,
                50 if binding.quantity == "soc" else 0,
                {"soc": "%", "energy": "kWh", "power": "W"}[binding.quantity],
                now,
                now,
                "ha_state_change",
            )
            for binding in configuration.bindings
        ),
        (),
        location,
    )
    if demand is None:
        definition = DemandConfiguration("Europe/Amsterdam", 8760, (EnergyTerm("sensor.house", 1),))
        demand = SimpleNamespace(
            store=SimpleNamespace(issue=None),
            configuration=definition,
            forecast=DemandForecast(
                definition.version,
                "independent-projection-fixture",
                now,
                "constant-hour-profile",
                load_steps
                if load_steps is not None
                else tuple(
                    ForecastStep(start, duration, duration) for start, duration in horizon(now)
                ),
                (),
            ),
        )
    source = InstallationBasisSource(SimpleNamespace(current=settings, issue=None), demand, solar)
    return source, configuration, report
