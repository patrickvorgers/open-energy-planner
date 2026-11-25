"""Publish appliance settings inputs when an application state transaction finishes."""

import asyncio
from dataclasses import dataclass

from aiohttp import web

from open_energy_planner.appliances.cycles.service import ApplianceView
from open_energy_planner.appliances.forecasting import COMPOSITE_MODEL_ID, ConsumerView

NO_COMMANDS = web.RequestKey("oep-response-contains-no-commands", bool)


def mark_command_free(request: web.Request) -> None:
    """Tell the app that a read does not convey authority requiring the revocation barrier."""
    request[NO_COMMANDS] = True


@dataclass(frozen=True)
class SettingsRead:
    """One published view of appliance state, forecasts and meter ownership."""

    appliances: ApplianceView
    consumers: ConsumerView
    charging_meters: tuple[str, ...]
    thermal_meters: tuple[str, ...]
    active_in_planning: bool

    def consumer_document(self, *, forecasts, now):
        """Expose Energy source choices along with their specialized owners."""
        return {
            **self.consumers.document(forecasts=forecasts, now=now),
            "charging_meters": sorted(self.charging_meters),
            "thermal_meters": sorted(self.thermal_meters),
            "active_in_planning": self.active_in_planning,
        }


class ApplicationStateLock(asyncio.Lock):
    """Capture settings after serialized writers have installed their committed state."""

    def bind_settings(self, appliances, demand, ev, thermal):
        """Publish the restored state before the first HTTP request or background write."""
        self._settings_sources = appliances, demand, ev, thermal
        self._publish_settings()

    def _publish_settings(self):
        """Swap bounded inputs without projection, version hashing or database access."""
        appliances, demand, ev, thermal = self._settings_sources
        self.settings = SettingsRead(
            appliances.read_view(),
            demand.consumers.read_view(),
            tuple(point["energy_entity"] for point in ev.state["points"]),
            tuple(store["energy_entity"] for store in thermal.state["stores"]),
            bool(demand.forecast and demand.forecast.model == COMPOSITE_MODEL_ID),
        )

    async def __aexit__(self, exc_type, exc, tb):
        """Publish installed inputs even after cancellation of an already committed write."""
        try:
            self._publish_settings()
        finally:
            self.release()
