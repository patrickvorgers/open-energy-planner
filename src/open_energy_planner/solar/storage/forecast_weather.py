"""Persist complete frozen forecast runs and their original retrieval receipts in the active solar store."""

from dataclasses import asdict
from datetime import datetime

from open_energy_planner.core.units import HOURS_PER_DAY
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version
from open_energy_planner.solar.forecast_weather import (
    FORECAST_RUN_RESPONSE_HOURS,
    HOUR,
    MAX_FORECAST_RUN_HOURS,
    SINGLE_RUNS_URL,
    ForecastWeatherRun,
)

MAX_RUN_BLOCKS = MAX_FORECAST_RUN_HOURS // HOURS_PER_DAY + 1
RECEIPT_FIELDS = {
    "query",
    "data_reference",
    "model",
    "initialized_at",
    "received_at",
    "available_at",
    "location_version",
    "policy_version",
}


class ForecastWeatherCache:
    """Retain immutable request/run/site evidence without advancing incomplete or corrupt pointers."""

    def __init__(self, store):
        """Borrow the app-owned solar namespace and shared active database engine."""
        self.store = store

    @staticmethod
    def _index(query):
        """Bind a cache pointer to the entire normalized provider request and simulation origin."""
        return "forecast-weather-index-" + version(query) + ".json"

    @staticmethod
    def _values(run):
        """Keep immutable physical values separate from their one actual retrieval receipt."""
        values = []
        for row in run.hours:
            value = asdict(row)
            value["start"] = row.start.isoformat()
            value.pop("received_at")
            values.append(value)
        return values

    def read(self, query, origin=None):
        """Restore complete original values and clocks, failing closed on any retained corruption."""
        pointer = self._index(query)
        if not self.store.documents.exists(pointer):
            return None
        try:
            reference = self.store.documents.read(pointer)
            if not isinstance(reference, str) or len(reference) != 64:
                raise ValueError("Invalid frozen solar forecast pointer")
            receipt = self.store.documents.read("forecast-weather-receipt-" + reference + ".json")
            if (
                not isinstance(receipt, dict)
                or set(receipt) != RECEIPT_FIELDS
                or version(receipt) != reference
                or receipt["query"] != query
            ):
                raise ValueError("Invalid frozen solar forecast receipt")
            data_reference = receipt["data_reference"]
            manifest = self.store.documents.read(
                "forecast-weather-data-" + data_reference + ".json"
            )
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"entity_id", "blocks"}
                or manifest["entity_id"] is not None
                or not isinstance(manifest["blocks"], list)
                or not 1 <= len(manifest["blocks"]) <= MAX_RUN_BLOCKS
            ):
                raise ValueError("Invalid frozen solar forecast manifest")
            values = []
            for block in manifest["blocks"]:
                body = self.store.documents.read("block-" + block["block"] + ".json")
                if (
                    not isinstance(body, dict)
                    or set(body) != {"entity_id", "hours"}
                    or version(body) != block["block"]
                    or body["entity_id"] is not None
                    or block["receipts"]
                    or not isinstance(body["hours"], list)
                ):
                    raise ValueError("Invalid frozen solar forecast block")
                values.extend(body["hours"])
                if len(values) > MAX_FORECAST_RUN_HOURS:
                    raise ValueError("Frozen solar forecast cache is too large")
            if version({"query": query, "hours": values}) != data_reference:
                raise ValueError("Frozen solar forecast values changed")
            received = datetime.fromisoformat(receipt["received_at"])
            rows = tuple(
                SolarWeatherHour(
                    **{**value, "start": datetime.fromisoformat(value["start"])},
                    received_at=received,
                )
                for value in values
            )
            run = ForecastWeatherRun(
                **{
                    field: receipt[field]
                    for field in ("model", "location_version", "policy_version")
                },
                initialized_at=datetime.fromisoformat(receipt["initialized_at"]),
                forecast_origin=origin,
                received_at=received,
                available_at=datetime.fromisoformat(receipt["available_at"])
                if receipt["available_at"] is not None
                else None,
                hours=rows,
            )
            self._matches(query, run)
            return run
        except (KeyError, TypeError, IndexError, OverflowError, AttributeError) as error:
            raise ValueError("Invalid retained frozen solar forecast") from error

    @staticmethod
    def _matches(query, run):
        """Reject source/model/time substitution even when its physical values happen to match."""
        if (
            query["url"] != SINGLE_RUNS_URL
            or query["params"]["models"] != run.model
            or query["params"]["run"] != run.initialized_at.strftime("%Y-%m-%dT%H:%M")
            or query["params"]["forecast_hours"] != FORECAST_RUN_RESPONSE_HOURS
            or query["model"] != run.model
            or query["initialized_at"] != run.initialized_at.isoformat()
            or query["location_version"] != run.location_version
            or query["policy"] != run.policy_version
            or len(run.hours) != FORECAST_RUN_RESPONSE_HOURS - 1
            or any(
                row.start != run.initialized_at + index * HOUR
                for index, row in enumerate(run.hours)
            )
        ):
            raise ValueError("Frozen solar forecast request mismatch")

    def write(self, query, run):
        """Install a fully validated first receipt atomically while preserving original retained evidence."""
        if not isinstance(run, ForecastWeatherRun):
            raise TypeError("Invalid frozen solar forecast run")
        self._matches(query, run)
        values = self._values(run)
        data_reference = version({"query": query, "hours": values})
        receipt = {
            "query": query,
            "data_reference": data_reference,
            "model": run.model,
            "initialized_at": run.initialized_at.isoformat(),
            "received_at": run.received_at.isoformat(),
            "available_at": run.available_at.isoformat() if run.available_at is not None else None,
            "location_version": run.location_version,
            "policy_version": run.policy_version,
        }
        reference = version(receipt)
        with self.store.transaction():
            previous = self.read(query, run.forecast_origin)
            if previous is not None:
                if self._values(previous) != values or previous.available_at != run.available_at:
                    raise ValueError("Frozen solar forecast changed")
                return previous
            self.store.archive_series("forecast-weather-data", data_reference, values)
            self.store.archive("forecast-weather-receipt", reference, receipt)
            self.store.documents.write(self._index(query), reference)
        return run
