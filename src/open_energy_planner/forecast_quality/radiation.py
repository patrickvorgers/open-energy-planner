"""Project frozen irradiance and retain independent satellite receipts in the active database."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiohttp import ClientError

from open_energy_planner.runtime.diagnostics.logging import BackgroundLog
from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.satellite import HOUR, SATELLITE_LABEL, SATELLITE_MODEL
from open_energy_planner.storage.writes import complete_write

SATELLITE_REFRESH_MINUTES = 10


def original_radiation(documents, forecast, start, end):
    """Resolve only hours received before issuance from the exact original weather manifest."""
    reference = forecast.get("weather_version")
    if not reference or reference == "none":
        return {}
    manifest = documents.read("weather-" + reference + ".json")
    issued = datetime.fromisoformat(forecast["issued_at"])
    values = {}
    for block in manifest["blocks"]:
        if not start.date().isoformat() <= block["day"] <= end.date().isoformat():
            continue
        hours = documents.read("block-" + block["block"] + ".json")["hours"]
        receipts = dict(block["receipts"])
        received = None
        for index, row in enumerate(hours):
            if index in receipts:
                received = datetime.fromisoformat(receipts[index])
            instant = datetime.fromisoformat(row["start"])
            if start <= instant and instant + HOUR <= end and received and received <= issued:
                values[instant] = row["ghi"]
    return values


def radiation_view(forecast, satellite, start, end, now, *, issue=None):
    """Keep gaps and compute coverage from the selected day's first complete UTC hour.

    The complete-through marker stops at the first missing observation, even
    when later observations exist. Fractional edge hours are not fabricated.
    Energy scores remain independent of this optional weather reference.
    """
    observed = {
        datetime.fromisoformat(row["start"]): row["ghi"]
        for row in satellite.get("hours", ())
        if datetime.fromisoformat(row["start"]) + HOUR <= min(end, now)
    }
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += HOUR
    rows, complete_until, contiguous = [], None, cursor == start
    while cursor + HOUR <= end:
        actual = observed.get(cursor)
        if contiguous and actual is not None:
            complete_until = cursor + HOUR
        else:
            contiguous = False
        rows.append(
            {
                "start": cursor.isoformat(),
                "end": (cursor + HOUR).isoformat(),
                "forecast_w_m2": forecast.get(cursor),
                "satellite_w_m2": actual,
            }
        )
        cursor += HOUR
    return {
        "quantity": "Global horizontal irradiance",
        "unit": "W/m²",
        "source": SATELLITE_LABEL,
        "dataset": SATELLITE_MODEL,
        "received_at": satellite.get("received_at"),
        "complete_until": complete_until.isoformat() if complete_until else None,
        "observed_hours": sum(row["satellite_w_m2"] is not None for row in rows),
        "hours": len(rows),
        "rows": rows,
        "issue": issue,
    }


class RadiationArchive:
    """Serialize bounded cache refreshes without holding the planning lock or altering forecasts."""

    def __init__(self, documents):
        """Borrow solar documents and require an explicitly opened satellite adapter."""
        self.documents, self.source = documents, None
        self.lock = asyncio.Lock()
        self.retry_after = {}
        self.status = BackgroundLog(logging.getLogger(__name__), "Satellite irradiance refresh")

    def save(self, key, receipt):
        """Commit immutable observations and their current pointer atomically."""
        with self.documents.transaction():
            name = "quality-satellite-receipt-" + version(receipt) + ".json"
            if not self.documents.exists(name):
                self.documents.write(name, receipt)
            self.documents.write(key, receipt)

    async def read(self, location, start, end, now):
        """Cache one site/day for ten minutes and retain explicitly stale evidence on failure."""
        key = (
            "quality-satellite-"
            + version(
                {
                    "location": location,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "dataset": SATELLITE_MODEL,
                }
            )
            + ".json"
        )
        async with self.lock:
            exists = await asyncio.to_thread(self.documents.exists, key)
            cached = await asyncio.to_thread(self.documents.read, key) if exists else {}
            try:
                if cached and now < datetime.fromisoformat(cached["received_at"]) + timedelta(
                    minutes=SATELLITE_REFRESH_MINUTES
                ):
                    return cached, None
            except (KeyError, TypeError, ValueError, OverflowError):
                cached = {}
            if self.source is None:
                return (
                    cached,
                    "Satellite observations are unavailable. Refresh after the weather connection is restored.",
                )
            if key in self.retry_after and now < self.retry_after[key]:
                return (
                    cached,
                    "Satellite refresh failed. Retained observations are shown. Retry after ten minutes.",
                )
            try:
                hours = await self.source.read(location, start, end, now)
                receipt = {
                    "location": location,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "dataset": SATELLITE_MODEL,
                    "received_at": now.isoformat(),
                    "hours": [{"start": row.start.isoformat(), "ghi": row.ghi} for row in hours],
                }
                cancellation = await complete_write(self.save, key, receipt)
                if cancellation:
                    raise cancellation
            except (ClientError, OSError, TypeError, ValueError, TimeoutError):
                self.retry_after[key] = now + timedelta(minutes=SATELLITE_REFRESH_MINUTES)
                self.status.exception(
                    "Forecast quality retains earlier satellite evidence. Energy evaluation and planning continue. Refresh again after ten minutes.",
                    expected=True,
                )
                return (
                    cached,
                    "Satellite refresh failed. Retained observations are shown. Retry after ten minutes.",
                )
            self.retry_after.pop(key, None)
            self.status.success("Satellite observations are available for forecast quality.")
            return receipt, None
